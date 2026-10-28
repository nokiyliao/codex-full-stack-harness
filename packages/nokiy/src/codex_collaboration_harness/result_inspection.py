# SPDX-License-Identifier: MIT
"""Bounded inspection and optional create-only handoff of one full-core request.

This is evidence inspection, not replay, process ownership, or mission acceptance.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
from typing import Any

from . import embedded_nokiy as caller
from . import full_core
from . import file_change_evidence
from . import postimage_context
from .verifier_parent import VERIFICATION_EVIDENCE_SCHEMA, _admitted_absent_creation, _verification_targets

MAX_JSON = 2 * 1024 * 1024
PAGE_SIZE = 16
MAX_SELECTED_COMMANDS = 8
MAX_PAGE_BYTES = 32768
MAX_COMMAND_BYTES = 2048
# Parent projections show a settled long command by its first line and identity only;
# the body stays in the inspection pages (inspect with an offset) and the event index.
MAX_COMMAND_HEAD_CHARS = 160
# Coordinator summaries carry usage totals only; coverage detail stays in terminal.json.
SUMMARY_USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
                        "total_tokens")
MAX_DIAGNOSTIC_BYTES = 512
MAX_PROJECTED_BYTES = 64 * 1024
MAX_REVIEW_CONTEXT_BYTES = 12 * 1024
INSPECTION_SUMMARY_FILE = "inspection-summary.json"
PARENT_REVIEW_INPUT_SCHEMA = "nokiy_parent_review_input_v1"
MAX_CAPABILITY_GAP_BYTES = 8192
MAX_CAPABILITY_DIFF_BYTES = 4096
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens",
                "reasoning_output_tokens", "cache_write_tokens", "latency_ms")
_LINE_LABEL = re.compile(r"([1-9][0-9]*): (.*)")
_BATCH_CALL = re.compile(r"(.+\.tool\.command_run:call_[^:]+):([0-9]+)")
_SHA = re.compile(r"[0-9a-f]{64}")


def _read_lines(output: dict[str, Any]) -> list[tuple[int, str]] | None:
    """Recover only returned physical lines; never infer a search-result interval."""
    text = output.get("stdout")
    start, end = output.get("start_line"), output.get("end_line")
    total = output.get("total_lines")
    numbered = output.get("line_numbers", False)
    truncated, reason = output.get("truncated"), output.get("truncation_reason")
    at_eof, next_line = output.get("at_eof"), output.get("next_line")
    ends_with_newline = output.get("ends_with_newline")
    if (not isinstance(text, str) or type(start) is not int or start < 1
            or type(end) is not int or end < 0 or type(numbered) is not bool
            or type(total) is not int or total < 0 or end > total
            or type(ends_with_newline) is not bool or type(truncated) is not bool
            or (truncated and reason not in ("end_of_file", "line_limit", "response_limit"))
            or (not truncated and reason is not None)
            or type(output.get("exit_code")) is not int
            or output["exit_code"] != 0
            or (next_line is not None and (type(next_line) is not int or next_line != end + 1))
            or type(at_eof) is not bool or (at_eof and next_line is not None)
            or (truncated and ((reason == "end_of_file") != at_eof))
            or (truncated and not at_eof and next_line is None)):
        return None
    matches = output.get("search_matches")
    if matches is not None and (not isinstance(matches, list) or
                                any(type(n) is not int or n < 1 or n > total for n in matches)
                                or not numbered):
        return None
    if not text and end == 0 and not truncated and (matches is None or not matches):
        return []
    # A cut-off response cannot establish the last physical line unless it
    # ended at a newline (or at a proven, unterminated end of the source).
    if (not text or (text.endswith("\n") != ends_with_newline)
            or (not ends_with_newline and
                (not at_eof or next_line is not None or end != total))):
        return None
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    if not lines or end < start:
        return None
    if numbered:
        parsed = []
        for line in lines:
            match = _LINE_LABEL.fullmatch(line)
            if match is None:
                return None
            parsed.append((int(match[1]), match[2]))
        numbers = [number for number, _ in parsed]
        if (numbers != sorted(set(numbers)) or numbers[0] < start or numbers[-1] > end
                or numbers[-1] != end):
            return None
        return parsed
    if len(lines) != end - start + 1:
        return None
    return list(zip(range(start, end + 1), lines))


class _ReadEfficiency:
    """Ephemeral, bounded projection from the already proof-checked command pass."""
    def __init__(self) -> None:
        self.calls = 0
        self.skipped = 0
        self.bytes = 0
        self.lines = 0
        self.repeated = 0
        self.seen: dict[tuple[str, str, int], tuple[str, str]] = {}
        self.overlap_counts = {"range_after_search": 0, "search_after_range": 0,
                               "range_after_range": 0, "search_after_search": 0,
                               "unknown_mode": 0}
        self.unknown_read_mode = False
        self.batches: set[str] = set()
        self.conflicts = 0

    def add(self, item: dict[str, Any], output: dict[str, Any], receipt: str) -> None:
        # command_run may contain several independently receipted source_read entries.
        entries = output.get("results") if isinstance(output.get("results"), list) else None
        candidates = entries if isinstance(entries, list) else [output]
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            payload = entry.get("output") if isinstance(entry.get("output"), dict) else entry
            if not isinstance(payload, dict):
                continue
            if (entry.get("command_type", item.get("command_type", item.get("command"))) != "source_read"
                    and payload.get("source_sha256") is None and payload.get("search_matches") is None):
                continue
            self.calls += 1
            proof = entry.get("terminal_receipt", payload.get("terminal_receipt"))
            # The enclosing command receipt must be matched. An entry receipt, if
            # present, must also prove a completed, successful call.
            if (receipt != "matched" or entry.get("success") is False
                    or (entries is not None and proof is None)
                    or (proof is not None and
                        (not isinstance(proof, dict)
                         or proof.get("schema_version") != "tura_command_terminal_receipt_v1"
                         or proof.get("outcome") != "known"
                         or proof.get("terminal_state") != "completed" or proof.get("exit_code") != 0
                         or any(proof.get(key) is not True for key in
                                ("process_reaped", "process_group_empty", "termination_proven"))))):
                self.skipped += 1
                continue
            path, sha = payload.get("path"), payload.get("source_sha256")
            returned = _read_lines(payload)
            if (not isinstance(path, str) or not path or not isinstance(sha, str)
                    or _SHA.fullmatch(sha) is None or returned is None
                    or ("path" in entry and entry["path"] != path)):
                self.skipped += 1
                continue
            call_id = proof.get("call_id") if isinstance(proof, dict) else None
            if call_id is None and entries is None:
                outer = output.get("terminal_receipt")
                call_id = outer.get("call_id") if isinstance(outer, dict) else None
            batch = _BATCH_CALL.fullmatch(call_id) if isinstance(call_id, str) else None
            if batch is None:
                self.skipped += 1
                continue
            if batch is not None:
                self.batches.add(batch[1])
            # Attribute only observations admitted by the existing proof,
            # identity and physical-line checks; ambiguous metadata stays unknown.
            mode = payload.get("mode")
            if (isinstance(payload.get("search_matches"), list)
                    and ("mode" not in payload or mode == "search")):
                kind = "search"
            elif ("search_matches" not in payload
                    and ("mode" not in payload or mode == "range")):
                kind = "range"
            else:
                kind = "unknown"
                self.unknown_read_mode = True
            self.bytes += sum(len(text.encode("utf-8")) for _, text in returned)
            self.lines += len(returned)
            for number, text in returned:
                key = (path, sha, number)
                previous = self.seen.get(key)
                if previous is None:
                    self.seen[key] = (text, kind)
                elif previous[0] == text:
                    self.repeated += 1
                    counter = (f"{kind}_after_{previous[1]}"
                               if kind != "unknown" and previous[1] != "unknown"
                               else "unknown_mode")
                    self.overlap_counts[counter] += 1
                    # Keep one source text, advancing only the last matching kind.
                    self.seen[key] = (previous[0], kind)
                else:
                    self.conflicts += 1

    def summary(self, index_complete: bool, proof_complete: bool) -> dict[str, Any]:
        coverage = ("complete" if index_complete and proof_complete
                    and not self.skipped and not self.conflicts else "unproven")
        return {"coverage": coverage, "source_read_calls": self.calls,
                "command_run_batches": len(self.batches), "source_bytes_excluding_newlines": self.bytes,
                "returned_lines": self.lines, "unique_lines": len(self.seen),
                "repeated_lines": self.repeated, "skipped_unproven": self.skipped,
                "conflicting_lines": self.conflicts, "index_complete": index_complete,
                "overlap_classification": {
                    "schema_version": "nokiy_source_read_overlap_v1",
                    "scope": "observed_command_outputs_only",
                    "coverage": ("complete" if coverage == "complete" and not self.unknown_read_mode
                                 else "unproven"),
                    **self.overlap_counts}}


class InspectionError(ValueError):
    def __init__(self, code: str, *, diagnostic: dict[str, Any] | None = None):
        self.code = code
        self.diagnostic = diagnostic
        super().__init__(code)


def _directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise InspectionError("DIRECTORY_UNAVAILABLE") from exc
    if not stat.S_ISDIR(mode):
        raise InspectionError("UNSAFE_DIRECTORY")


def _file(path: Path, limit: int, *, dir_fd: int | None = None) -> bytes:
    try:
        mode = (path.lstat() if dir_fd is None else
                os.stat(path, dir_fd=dir_fd, follow_symlinks=False)).st_mode
        if not stat.S_ISREG(mode):
            raise InspectionError("UNSAFE_FILE")
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                               dir_fd=dir_fd), "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                raise InspectionError("UNSAFE_OR_OVERSIZED_FILE")
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise InspectionError("OVERSIZED_FILE")
        return raw
    except OSError as exc:
        raise InspectionError("FILE_UNAVAILABLE") from exc


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=caller._unique_object,
                           parse_constant=caller._invalid_constant)
    except (UnicodeError, ValueError, RecursionError, caller.EmbeddedNokiyError) as exc:
        raise InspectionError("INVALID_JSON") from exc
    if not isinstance(value, dict):
        raise InspectionError("INVALID_JSON")
    return value


def _artifact(run: Path, name: str, ref: object, limit: int) -> tuple[bytes, dict[str, Any]]:
    path = run / name
    ref = full_core._artifact_reference(path, ref)
    if ref["bytes"] > limit:
        raise InspectionError("UNSAFE_OR_OVERSIZED_FILE")
    # Only fixed-size JSON artifacts use this bounded in-memory projection.
    with full_core._ArtifactReader(path, limit=limit, reference=ref) as source:
        raw = b"".join(source.chunks())
    return raw, {"path": name, "sha256": source.record["sha256"], "bytes": source.record["bytes"]}


def _receipt_locator(value: str, *, path: bool = False) -> str | dict[str, Any]:
    """Bound locator metadata; never truncate an identity into a different one."""
    raw = value.encode("utf-8", errors="replace")
    if (len(raw) <= (1024 if path else 512)
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in "\u2028\u2029" for c in value)
            and (path or re.fullmatch(r"[A-Za-z0-9_.:-]+", value))):
        return value
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _receipt_observed(receipt: dict[str, Any], field: str) -> dict[str, Any]:
    """Finite vocabulary plus small typed scalars, never arbitrary receipt text."""
    if field not in receipt:
        return {"type": "missing"}
    value = receipt[field]
    if value is None:
        return {"type": "null"}
    if type(value) is bool:
        return {"type": "bool", "value": value}
    if type(value) is int:
        return {"type": "int", **({"value": value} if -(2**31) <= value < 2**31 else {"bounded": False})}
    if isinstance(value, str):
        allowed = {"schema_version": ("tura_command_terminal_receipt_v1",),
                   "outcome": ("known", "unknown"),
                   "terminal_state": ("completed", "failed", "cancelled", "running", "unknown")}
        return {"type": "str", **({"value": value} if value in allowed.get(field, ()) else {"redacted": True})}
    return {"type": "object" if isinstance(value, dict) else "array" if isinstance(value, list) else "number"}


def _receipt(run: Path, output: dict[str, Any], *, item: dict[str, Any] | None = None,
             event_index: int | None = None, result_index: int | None = None) -> str:
    value, name = output.get("terminal_receipt"), output.get("terminal_receipt_path")
    if value is None and name is None:
        return "unavailable"
    if not isinstance(value, dict) or not isinstance(name, str):
        raise InspectionError("RECEIPT_INCOMPLETE")
    directory = run / "execution-state" / "command_receipts"
    _directory(run / "execution-state")
    _directory(directory)
    path = Path(name)
    if (path.parent != directory or path.suffix != ".json" or path.name in {"", ".", ".."}
            or not path.is_absolute()):
        raise InspectionError("RECEIPT_PATH_INVALID")
    actual = _json(_file(path, MAX_JSON))
    if actual != value:
        raise InspectionError("RECEIPT_MISMATCH")
    # Integrity proves a known, settled outcome, not necessarily success. Check
    # both copies: dict equality alone treats bools and integers as equal.
    for copy, receipt in (("file", actual), ("embedded", value)):
        code = receipt.get("exit_code")
        checks = [("schema_version", "equals", "tura_command_terminal_receipt_v1",
                   receipt.get("schema_version") == "tura_command_terminal_receipt_v1"),
                  ("outcome", "equals", "known", receipt.get("outcome") == "known"),
                  ("exit_code", "type_is_int", "int", type(code) is int),
                  ("terminal_state", "equals", "completed" if code == 0 else "failed",
                   receipt.get("terminal_state") == ("completed" if code == 0 else "failed"))]
        checks.extend((key, "is_true", True, receipt.get(key) is True) for key in
                      ("process_reaped", "process_group_empty", "termination_proven"))
        for field, predicate, expected, passed in checks:
            if passed:
                continue
            identity = {key: _receipt_locator(item[key]) for key in
                        ("command_id", "command_run_id", "runtime_id", "provider_tool_call_id")
                        if isinstance(item, dict) and isinstance(item.get(key), str)}
            if isinstance(item, dict) and type(item.get("command_index")) is int and 0 <= item["command_index"] < 128:
                identity["command_index"] = item["command_index"]
            diagnostic = {"schema_version": "nokiy_receipt_diagnostic_v1", "code": "RECEIPT_PROOF_INVALID",
                          "request_locator": {"request_id": _receipt_locator(run.name),
                                              "artifact_root": _receipt_locator(str(run.parent), path=True)},
                          "identity": identity, "receipt_path": _receipt_locator(str(path), path=True),
                          "receipt_copy": copy, "failed_field": field, "failed_predicate": predicate,
                          "expected": expected, "observed": _receipt_observed(receipt, field)}
            for key, index in (("event_index", event_index), ("result_index", result_index)):
                if type(index) is int and 0 <= index < 2**31:
                    diagnostic[key] = index
            raise InspectionError("RECEIPT_PROOF_INVALID", diagnostic=diagnostic)
    return "matched"


def _compact_utf8_json(value: object) -> bytes:
    # The router's command/witness identities use UTF-8, not the caller's
    # ASCII-escaped request identity encoding.
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _source_read_arguments(line: str) -> tuple[dict[str, Any] | None, str | None]:
    """Recognize argument rejection kinds, never content or permission failures.

    A malformed hash is distinct from a valid hash that does not match a file.
    The durable router witness still has to attest the exact rejection kind.
    Positional fields are decoded only for classification; command identity
    remains bound to the original raw argument string.
    """
    try:
        args = json.loads(line, object_pairs_hook=caller._unique_object,
                          parse_constant=caller._invalid_constant)
    except (json.JSONDecodeError, caller.EmbeddedNokiyError):
        return None, "json_syntax_or_shape"
    except (UnicodeError, ValueError, RecursionError):
        return None, None
    # Mirror SourceReadRequest's deny_unknown_fields before semantic checks.
    fields = ("path", "start_line", "end_line", "expected_sha256", "line_numbers",
              "search_terms", "context_lines", "json_pointer", "expected_sha256_ref", "children_offset")
    if isinstance(args, list):
        if not 1 <= len(args) <= 8:
            return None, "json_syntax_or_shape"
        # Omitted optional fields default in serde; a missing range is semantic.
        args = dict(zip(fields, args))
    if not isinstance(args, dict):
        return None, None
    if set(args) - set(fields) or "path" not in args or not isinstance(args["path"], str):
        return args, "json_syntax_or_shape"
    # A present json_pointer is deserialized as String, unlike Option fields.
    if "json_pointer" in args and not isinstance(args["json_pointer"], str):
        return args, "json_syntax_or_shape"
    for key in ("start_line", "end_line", "context_lines", "children_offset"):
        value = args.get(key)
        if value is not None and (type(value) is not int or not 0 <= value < 2**64):
            return args, "json_syntax_or_shape"
    if ((args.get("line_numbers") is not None and type(args["line_numbers"]) is not bool)
                 or (args.get("expected_sha256") is not None and not isinstance(args["expected_sha256"], str))
                 or (args.get("expected_sha256_ref") is not None and not isinstance(args["expected_sha256_ref"], str))
                 or (args.get("search_terms") is not None and (
                     not isinstance(args["search_terms"], list)
                     or any(not isinstance(term, str) for term in args["search_terms"])))):
        return args, "json_syntax_or_shape"
    pin = args.get("expected_sha256")
    if (isinstance(pin, str) and re.fullmatch(r"[0-9a-fA-F]{64}", pin) is None
            and len(args["path"].encode("utf-8")) <= 512
            and _normalized_source_read(_compact_utf8_json({**args, "expected_sha256": "0" * 64}).decode()) is not None):
        return args, "invalid_expected_sha256"
    return args, None


def _rejected_source_read_command(item: dict[str, Any], rejection_kind: object) -> dict[str, Any] | None:
    """Reconstruct one exact command, never guess omitted optional metadata."""
    kind, line = item.get("command_type"), item.get("command")
    if (rejection_kind not in ("json_syntax_or_shape", "invalid_expected_sha256")
            or not isinstance(line, str) or len(line.encode("utf-8")) > 16384
            or kind not in (None, "source_read", "command_run")):
        return None
    declared, rejected = _source_read_arguments(line)
    wrapped = kind == "command_run" or (declared is not None and "commands" in declared)
    if wrapped:
        if (kind == "source_read" or declared is None or set(declared) != {"commands"}
                or not isinstance(declared["commands"], list) or len(declared["commands"]) != 1
                or not isinstance(declared["commands"][0], dict)):
            return None
        command = declared["commands"][0]
    else:
        # item.id is an event identifier, NOT the optional command id. If that
        # command id was projected away, the hash will not match this candidate.
        command = {"command_type": "source_read", "command_line": line,
                   **{key: item[key] for key in ("step", "timeout_ms", "stall_timeout_ms") if key in item}}
    semantic = {"command_type", "command_line", "step", "id", "timeout_ms", "stall_timeout_ms"}
    reporting = {"command_id", "command_run_id", "provider_tool_call_id", "command_index"}
    if (set(command) - semantic - reporting - {"command"}
            or command.get("command_type") != "source_read"
            or command.get("command", "source_read") != "source_read"
            or not isinstance(command.get("command_line"), str)
            or any(type(command[key]) is not int or not 0 < command[key] < 2**64
                   for key in ("step", "timeout_ms", "stall_timeout_ms") if key in command)
            or ("id" in command and (not isinstance(command["id"], str) or not command["id"].strip()))):
        return None
    if wrapped:
        _, rejected = _source_read_arguments(command["command_line"])
    if rejected != rejection_kind:
        return None
    return {**{key: value for key, value in command.items() if key in semantic},
            "step": command.get("step", 1)}


def _command_call_ids(item: dict[str, Any], output: dict[str, Any]) -> set[str]:
    # Count each identity once per event, including untrusted claims and ordinary
    # execution receipts. A later reused call must invalidate an earlier witness.
    values = [item, output, output.get("pre_execution_rejection"), output.get("terminal_receipt")]
    entries = output.get("results")
    for entry in entries if isinstance(entries, list) else ():
        if isinstance(entry, dict):
            payload = entry.get("output")
            values.extend((entry, entry.get("terminal_receipt"), payload))
            if isinstance(payload, dict):
                values.extend((payload.get("pre_execution_rejection"), payload.get("terminal_receipt")))
    return {value["call_id"] for value in values
            if isinstance(value, dict) and isinstance(value.get("call_id"), str)}


def _source_read_rejection(run: Path, item: dict[str, Any], output: dict[str, Any],
                           original: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Authenticate a non-attempt, not a successful read or verifier execution."""
    value = output.get("pre_execution_rejection")
    keys = {"schema_version", "owner", "rejection_kind", "session_id", "runtime_id", "execution_id",
            "call_id", "authorization_semantic_sha256", "command_sha256", "step", "error_message",
            "effect_state", "process_started", "source_content_read", "mutation_count", "authority_effect"}
    command = _rejected_source_read_command(item, value.get("rejection_kind") if isinstance(value, dict) else None)
    scopes, allowed, denied = (contract.get("read_scopes"), contract.get("allowed_operations"),
                               contract.get("denied_operations", []))
    if (set(output) != {"pre_execution_rejection"} or not isinstance(value, dict) or set(value) != keys
            or command is None or item.get("status") != "failed" or item.get("exit_code") is not None
            or item.get("success", False) is not False
            or any(key in item for key in ("results", "changes", "file_changes", "postimage", "postimages",
                                           "source_postimages", "terminal_receipt", "terminal_receipt_path",
                                           "stdout", "stderr"))
            or value["schema_version"] != "nokiy_source_read_pre_execution_rejection_v1"
            or value["owner"] != "router"
            or value["session_id"] != "full-" + original["request_sha256"]
            or any(not isinstance(value[key], str) or not value[key].strip() or len(value[key].encode("utf-8")) > 256
                   for key in ("runtime_id", "execution_id", "call_id"))
            # A session may have many runtimes. The final marker's runtime is
            # not the owner of every earlier command. Bind this singleton call.
            or not value["call_id"].startswith(value["runtime_id"] + ".tool.command_run:")
            or value["execution_id"] != value["call_id"]
            or any(key in item and item[key] != value[key]
                   for key in ("session_id", "runtime_id", "execution_id", "call_id"))
            or not isinstance(value["error_message"], str) or not 0 < len(value["error_message"].encode("utf-8")) <= 1024
            or type(value["step"]) is not int or not 0 < value["step"] < 2**64
            or value["step"] != command.get("step", 1)
            or value["effect_state"] != "not_started" or value["process_started"] is not False
            or value["source_content_read"] is not False or type(value["mutation_count"]) is not int
            or value["mutation_count"] != 0 or value["authority_effect"] != "none"
            or any(key in item and (type(item[key]) is not type(value[key]) or item[key] != value[key])
                   for key in ("effect_state", "process_started", "source_content_read", "mutation_count", "authority_effect"))
            or contract.get("source_read") is not True or not isinstance(allowed, list) or "read" not in allowed
            or not isinstance(denied, list) or "read" in denied
            or contract.get("repo_root") != original.get("workspace")
            or not isinstance(scopes, list) or not scopes or any(not isinstance(p, str) or not p for p in scopes)
            or not isinstance(contract.get("authorization_semantic_sha256"), str)
            or _SHA.fullmatch(contract["authorization_semantic_sha256"]) is None
            or value["authorization_semantic_sha256"] != contract["authorization_semantic_sha256"]
            or value["command_sha256"] != hashlib.sha256(_compact_utf8_json(command)).hexdigest()):
        raise InspectionError("SOURCE_READ_REJECTION_INVALID")
    claimed_raw = _compact_utf8_json(value)
    if len(claimed_raw) > 4096:
        raise InspectionError("SOURCE_READ_REJECTION_INVALID")
    identity = [value[key] for key in ("session_id", "runtime_id", "execution_id", "call_id")]
    name = "source-read-preexecution-" + hashlib.sha256(_compact_utf8_json(identity)).hexdigest() + ".json"
    # Pin the physical parents and open the witness without following any symlink.
    with _summary_directory(run) as run_fd:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        state_fd = os.open("execution-state", flags, dir_fd=run_fd)
        try:
            receipts_fd = os.open("command_receipts", flags, dir_fd=state_fd)
            try:
                raw = _file(Path(name), 4096, dir_fd=receipts_fd)
            finally:
                os.close(receipts_fd)
        finally:
            os.close(state_fd)
    # Byte comparison of canonical JSON, unlike dict equality, cannot equate
    # booleans and integers. The retained hash also binds the exact witness bytes.
    if _compact_utf8_json(_json(raw)) != claimed_raw:
        raise InspectionError("SOURCE_READ_REJECTION_MISMATCH")
    return {**{key: value[key] for key in value if key != "error_message"}, "receipt_check": "matched",
            "receipt": {"path": "execution-state/command_receipts/" + name,
                        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}}


_JSPACE_REFUSAL_KEYS = {
    "schema_version", "owner", "session_id", "runtime_id", "execution_id", "call_id",
    "authorization_semantic_sha256", "command_type", "command_line_sha256", "step", "jspace_error_code",
    "operation", "target", "error_message", "effect_state", "process_started", "mutation_count",
    "authority_effect"}


def _jspace_refusal(run: Path, item: dict[str, Any], original: dict[str, Any],
                    contract: dict[str, Any]) -> dict[str, Any]:
    """Authenticate a J-Space refusal as a non-attempt from the router's durable witness.

    The router refuses a streamed command before dispatch and records why under its call
    identity. The witness must describe exactly this command line, step and refusal text."""
    error, call, runtime, line = (item.get("aggregated_output"), item.get("command_id"),
                                  item.get("runtime_id"), item.get("command"))
    step = item.get("step", 1)
    if (item.get("command_type") != "jspace" or item.get("status") != "failed"
            or item.get("exit_code") is not None or item.get("success", False) is not False
            or not isinstance(error, str) or not error.startswith("JSPACE_")
            or not isinstance(line, str) or not isinstance(runtime, str) or not runtime.strip()
            or not isinstance(call, str) or not call.startswith(runtime + ".tool.command_run:")
            or type(step) is not int or not 0 < step < 2**64
            or any(key in item for key in ("results", "changes", "file_changes", "terminal_receipt",
                                           "terminal_receipt_path", "stdout", "stderr"))):
        raise InspectionError("JSPACE_REFUSAL_INVALID")
    session = "full-" + original["request_sha256"]
    name = "jspace-policy-denial-" + hashlib.sha256(_compact_utf8_json([session, runtime, call])).hexdigest() + ".json"
    # Pin the physical parents and open the witness without following any symlink.
    with _summary_directory(run) as run_fd:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        state_fd = os.open("execution-state", flags, dir_fd=run_fd)
        try:
            receipts_fd = os.open("command_receipts", flags, dir_fd=state_fd)
            try:
                raw = _file(Path(name), 32768, dir_fd=receipts_fd)
            finally:
                os.close(receipts_fd)
        finally:
            os.close(state_fd)
    value = _json(raw)
    authorization = contract.get("authorization_semantic_sha256")
    if (not isinstance(value, dict) or set(value) != _JSPACE_REFUSAL_KEYS
            or value["schema_version"] != "nokiy_jspace_policy_denial_v1" or value["owner"] != "router"
            or value["session_id"] != session or value["runtime_id"] != runtime or value["call_id"] != call
            or value["error_message"] != error or type(value["step"]) is not int or value["step"] != step
            or value["command_line_sha256"] != hashlib.sha256(line.encode("utf-8")).hexdigest()
            or not isinstance(value["jspace_error_code"], str) or not error.startswith(value["jspace_error_code"])
            or value["effect_state"] != "not_started" or value["process_started"] is not False
            or type(value["mutation_count"]) is not int or value["mutation_count"] != 0
            or value["authority_effect"] != "none"
            or not isinstance(authorization, str) or value["authorization_semantic_sha256"] != authorization):
        raise InspectionError("JSPACE_REFUSAL_MISMATCH")
    return {"call_id": call, "jspace_error_code": value["jspace_error_code"], "operation": value["operation"],
            "target": value["target"], "effect_state": "not_started", "receipt_check": "matched",
            "receipt": {"path": "execution-state/command_receipts/" + name,
                        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}}


def _process(pid: object) -> str:
    if type(pid) is not int or pid <= 0:
        return "unknown"
    try:
        os.kill(pid, 0)  # Observation only. PID reuse cannot establish ownership.
    except ProcessLookupError:
        return "absent"
    except (PermissionError, OSError):
        return "permission_unknown"
    return "present_unowned"


def _supervisor_process(scope: dict[str, Any], *, bound: bool) -> str:
    observed = _process(scope.get("supervisor_pid"))
    if (bound and scope.get("isolation_source") == "native_command_root"
            and scope.get("supervisor_pid") == os.getpid()):
        try:
            # This caller is still rendering its result, not running the engine.
            # Recheck the exclusive kernel scope; a PID or receipt label is not proof.
            if full_core.inherited_root_parent() is not None:
                return "current_root_settled"
        except (OSError, full_core.ProcessScopeError):
            pass
    return observed


def _cleanup_settled(cleanup: dict[str, Any]) -> bool:
    return (cleanup.get("historical_pass") is True and cleanup.get("engine_pid") == "absent"
            and cleanup.get("supervisor_pid") in ("absent", "current_root_settled"))


_NATIVE_COMMAND_IDENTITY = {"runtime_id", "step", "command_index", "command_id",
                            "command_run_id", "provider_tool_call_id"}


def _native_command_identity(item: dict) -> bool:
    runtime, call, run = (item.get(key) for key in ("runtime_id", "provider_tool_call_id", "command_run_id"))
    index, step = item.get("command_index"), item.get("step")
    return (_NATIVE_COMMAND_IDENTITY <= item.keys()
            and isinstance(runtime, str) and re.fullmatch(r"runtime-[A-Za-z0-9-]+", runtime) is not None
            and isinstance(call, str) and re.fullmatch(r"call_[A-Za-z0-9_-]+", call) is not None
            and run == runtime + ".tool.command_run"
            and type(index) is int and 0 <= index < 2**32
            and type(step) is int and 0 < step < 2**32
            and item.get("command_id") == f"{run}:{call}:{index}")


def _native_task_status(event: dict, command_ids: dict[str, int]) -> bool:
    """Only exact native bookkeeping acknowledgements may bypass effect barriers."""
    item = event["item"]
    if (event.get("type") != "item.completed" or item.get("status") != "completed"
            or item.get("exit_code") is not None or item.get("command_type") not in (None, "task_status")
            or set(item) - {"type", "id", "status", "exit_code", "command", "aggregated_output",
                            "command_type"} - _NATIVE_COMMAND_IDENTITY
            or _NATIVE_COMMAND_IDENTITY.intersection(item) and (
                not _native_command_identity(item) or command_ids.get(item["command_id"]) != 1)):
        return False
    try:
        output = full_core._structured_output(item)
    except (caller.EmbeddedNokiyError, ValueError, RecursionError):
        return False
    control = output.get("task_status")
    if (set(output) != {"task_status"} or not isinstance(control, dict)
            or set(control) - {"status", "task_type", "task_group", "compact_context",
                               "final_reply", "final_reply_format"}
            or "status" in control and control["status"] not in ("doing", "question", "done")
            or "task_group" in control and not isinstance(control["task_group"], str)
            or "compact_context" in control and not isinstance(control["compact_context"], str)
            or "task_type" in control and (not isinstance(control["task_type"], list)
                or not all(isinstance(kind, str) for kind in control["task_type"]))
            or not isinstance(item.get("command"), str)):
        return False
    try:
        command = _json(item["command"].encode())
        if "final_reply" in control or "final_reply_format" in control:
            if control.get("status") != "done" or "compact_context" in control:
                return False
            reply = command.get("final_reply")
            if isinstance(reply, str):
                if not reply.strip() or "final_reply_format" in control:
                    return False
            elif isinstance(reply, (dict, list)) and reply:
                if control.get("final_reply_format") != "json" or not isinstance(control.get("final_reply"), str):
                    return False
                control = {key: value for key, value in control.items() if key != "final_reply_format"}
                control["final_reply"] = json.loads(control["final_reply"], object_pairs_hook=caller._unique_object,
                                                    parse_constant=caller._invalid_constant)
            else:
                return False
        # Canonical bytes preserve JSON types, unlike Python's True == 1 equality.
        return caller._canonical_bytes(command) == caller._canonical_bytes(control)
    except (InspectionError, caller.EmbeddedNokiyError, UnicodeError, ValueError, RecursionError):
        return False


def _postgate_policy_denial(item: dict, original: dict, contract: dict) -> bool:
    """Native typed no-start evidence permits another verifier, never reconciles failure."""
    denial = item.get("policy_denial")
    fields = {"jspace_error_code", "operation", "target", "effect_state", "mutation_count",
              "authority_effect", "delivery_state", "replayable"}
    if (not isinstance(denial, dict) or set(denial) != fields
            or denial.get("jspace_error_code") != "JSPACE_SOURCE_READ_PATH_DENIED"
            or denial.get("operation") != "read" or denial.get("effect_state") != "not_started"
            or type(denial.get("mutation_count")) is not int or denial["mutation_count"] != 0
            or denial.get("authority_effect") != "none"
            or denial.get("delivery_state") != "deterministic_policy_denial"
            or denial.get("replayable") is not True
            or item.get("type") != "command_execution" or item.get("command_type") != "jspace"
            or item.get("status") != "failed" or item.get("exit_code") is not None
            or not isinstance(item.get("command"), str)
            or not isinstance(item.get("aggregated_output"), str)
            or set(item) - {"id", "type", "command", "aggregated_output", "exit_code", "status",
                            "command_type", "runtime_id", "step", "command_index", "command_id",
                            "command_run_id", "provider_tool_call_id", "policy_denial"}):
        return False
    args, rejection = _source_read_arguments(item["command"])
    target = denial["target"]
    if (args is None or rejection is not None or args.get("path") != target
            or not isinstance(target, str) or "\\" in target or "\0" in target
            or any(part in ("", ".", "..", ".git") for part in target.split("/"))
            or contract.get("repo_root") != original.get("workspace")
            or contract.get("source_read") is not True
            or not {"read", "command"}.issubset(contract.get("allowed_operations", []))
            or {"read", "command"}.intersection(contract.get("denied_operations", []))):
        return False
    scopes = contract.get("read_scopes")
    if (not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes)
            or not any(target == scope or scope.endswith("/**") and target.startswith(scope[:-3] + "/")
                       for scope in scopes)):
        return False
    return _native_command_identity(item)


def _command_selection(values: object) -> tuple[str, ...]:
    if (not isinstance(values, (tuple, list)) or len(values) > MAX_SELECTED_COMMANDS
            or any(not isinstance(value, str) or len(value) != 64
                   or any(char not in "0123456789abcdef" for char in value)
                   for value in values)
            or len(set(values)) != len(values)):
        raise InspectionError("COMMAND_SELECTION_INVALID")
    return tuple(values)


def _settled_output(value: dict[str, Any]) -> bool:
    return (value.get("success", True) is True and value.get("isError", False) is False
            and value.get("status") in (None, "completed")
            and value.get("outcome") in (None, "known")
            and value.get("terminal_state") in (None, "completed"))


def _command_head(command: str) -> str:
    return command.split("\n", 1)[0][:MAX_COMMAND_HEAD_CHARS]


def _parent_command_row(row: dict[str, Any]) -> dict[str, Any]:
    """One command as the coordinator sees it.

    Failures, rejections and verifier rows keep their body: the parent needs them to judge
    the result. A settled long or multi-line command is shown by its first line, hash and
    size; the full body stays in the inspection pages and the indexed event.
    """
    projected = {key: row[key] for key in ("event_index", "command", "exit_code",
                                          "receipt_check", "execution_proof")}
    command = row["command"]
    settled = (row["execution_proof"] == "known_success" and "failure_resolution" not in row
               and "pre_execution_rejection" not in row and "verifier_output_excerpt" not in row)
    if command is None or (settled and (len(command) > MAX_COMMAND_HEAD_CHARS or "\n" in command)):
        projected["command"] = None
        head = row.get("command_head") if command is None else _command_head(command)
        if isinstance(head, str):
            projected["command_head"] = head
        projected["command_sha256"] = row["command_sha256"]
        projected["command_bytes"] = row["command_bytes"]
    if "evidence_source" in row:
        projected.update(evidence_source=row["evidence_source"], command_id=row["command_id"])
    if "first_blocker_detail" in row:
        projected["first_blocker_detail"] = row["first_blocker_detail"]
    if "failure_resolution" in row:
        projected["failure_resolution"] = row["failure_resolution"]
    if "verifier_recovery_observation" in row:
        projected["verifier_recovery_observation"] = row["verifier_recovery_observation"]
    if "pre_execution_rejection" in row:
        projected["pre_execution_rejection"] = row["pre_execution_rejection"]
    if "verifier_output_excerpt" in row:
        projected["diagnostic_excerpt"] = row["verifier_output_excerpt"]
    elif (row["execution_proof"] == "known_failure" and isinstance(row.get("diagnostic_excerpt"), str)
          and row["diagnostic_excerpt"]):
        projected["diagnostic_excerpt"] = row["diagnostic_excerpt"].encode("utf-8", errors="replace")[
            :MAX_DIAGNOSTIC_BYTES].decode("utf-8", errors="ignore")
    return projected


# rg, grep and git grep exit 1 only when nothing matched; their errors exit 2.
_SEARCH_PROGRAMS = frozenset({"rg", "grep", "egrep", "fgrep"})
_SHELL_OPERATOR = frozenset("();<>|&")


def _search_no_match(command: Any, output: dict[str, Any]) -> bool:
    """A search that ran to completion and matched nothing: its exit 1 is an answer.

    Only one simple command qualifies, with an authenticated workload exit and no
    stderr. A shell operator, substitution or expansion keeps the failure, since the
    status could then come from another program."""
    receipt = output.get("terminal_receipt")
    if (not isinstance(command, str) or output.get("stderr") != ""
            or not isinstance(receipt, dict) or receipt.get("termination_origin") != "workload"
            or any(marker in command for marker in ("`", "$(", "${", "\n", "\r"))):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        argv = list(lexer)
    except ValueError:
        return False
    if not argv or any(token and set(token) <= _SHELL_OPERATOR for token in argv):
        return False
    program = os.path.basename(argv[0])
    return program in _SEARCH_PROGRAMS or (program == "git" and argv[1:2] == ["grep"])


# The runtime's in-process source_read receipt for a failure that returned no content.
_EFFECT_FREE_READ_RECEIPT = {
    "termination_origin": "in_process_source_read", "pid": None, "outcome": "known",
    "terminal_state": "failed", "exit_code": 1, "authority_effect": "none", "staging_authority": "none",
    "authoritative_publication": "unproven", "termination_proven": True, "process_reaped": True,
    "process_group_empty": True, "reconcile_required": True, "retry_safe": False,
    "auto_retry_allowed": False,
}


def _source_read_digest_mismatch(item: dict[str, Any], command: Any, output: dict[str, Any]) -> bool:
    """A read whose well-formed expected_sha256 did not match the file (for example a digest
    copied from another file) ended in-process before returning any content: no effect.

    Like a no-match search this explains its own failed record. It settles execution
    certainty only; the read stays failed and no content or dependency proof is inferred."""
    receipt, evidence = output.get("terminal_receipt"), output.get("source_read_evidence")
    if (item.get("command_type") != "source_read" or not isinstance(command, str)
            or set(output) - {"error_type", "exit_code", "source_read_evidence", "stderr", "stdout",
                              "terminal_receipt", "terminal_receipt_path"}
            or output.get("error_type") != "SOURCE_READ_SHA256_MISMATCH"
            or output.get("stdout") != "" or output.get("stderr") != "SOURCE_READ_SHA256_MISMATCH"
            or not isinstance(receipt, dict) or not isinstance(evidence, dict)
            or receipt.get("source_read_evidence") != evidence
            or any(receipt.get(key) != value or (value is not None and type(receipt.get(key)) is not type(value))
                   for key, value in _EFFECT_FREE_READ_RECEIPT.items())
            or evidence.get("schema_version") != "nokiy_source_read_content_v1"
            or evidence.get("content_satisfied") is not False):
        return False
    request, actual = evidence.get("request"), evidence.get("source_sha256")
    expected = request.get("expected_sha256") if isinstance(request, dict) else None
    if (not isinstance(expected, str) or _SHA.fullmatch(expected) is None
            or not isinstance(actual, str) or _SHA.fullmatch(actual) is None or actual == expected):
        return False
    try:
        line = json.loads(command)
    except (ValueError, RecursionError):
        return False
    if isinstance(line, list) and len(line) == 4:
        line = dict(zip(("path", "start_line", "end_line", "expected_sha256"), line))
    if not isinstance(line, dict):
        return False
    return (line.get("path") == request.get("path") and line.get("expected_sha256") == expected
            and all(line[key] == request.get(key) for key in ("start_line", "end_line") if key in line))


def _outside_write_scope(path: Any, contract: dict[str, Any]) -> bool:
    """Writable paths stay with the own-edit read rules: there a stale pin means the worker
    has not observed its own change. Any non-exact write scope keeps every path writable."""
    scopes = [scope for key in ("write_scopes", "declared_targets") for scope in contract.get(key) or ()]
    return (isinstance(path, str) and bool(path)
            and all(isinstance(scope, str) and "*" not in scope for scope in scopes) and path not in scopes)


_INSPECT_SHELL_KINDS = frozenset({"zsh", "bash", "shell_command"})
# The runtime's receipt for a shell workload that exited non-zero and was fully reaped.
_CLOSED_SHELL_FAILURE_RECEIPT = {
    "termination_origin": "workload", "failure_class": "workload_exit_nonzero", "outcome": "known",
    "terminal_state": "failed", "authority_effect": "none", "staging_authority": "none",
    "authoritative_publication": "unproven", "termination_proven": True, "process_reaped": True,
    "process_group_empty": True, "reconcile_required": True,
}


def _inspect_shell_failure(item: dict[str, Any], output: dict[str, Any]) -> bool:
    """A shell line that exited non-zero and was fully reaped without any recorded effect.

    Under an admitted inspect contract the bound Seatbelt profile confines such shells to the
    task-local evidence root, so the exit is a finding (a crashed probe, a failing check), not an
    unknown effect. Like a no-match search it explains its own failed record only."""
    receipt, exit_code = output.get("terminal_receipt"), output.get("exit_code")
    return (item.get("command_type") in _INSPECT_SHELL_KINDS
            and not set(output) - {"exit_code", "message", "stderr", "stdout",
                                   "terminal_receipt", "terminal_receipt_path"}
            and type(exit_code) is int and exit_code != 0
            and isinstance(receipt, dict) and type(receipt.get("exit_code")) is int
            and receipt["exit_code"] == exit_code
            and all(receipt.get(key) == value and type(receipt.get(key)) is type(value)
                    for key, value in _CLOSED_SHELL_FAILURE_RECEIPT.items())
            and all(receipt.get(key) in (None, False) for key in ("timed_out", "cancelled"))
            and receipt.get("error") in (None, "")
            and all(receipt.get(key) in (None, []) for key in ("unresolved_effects", "apply_patch_changes")))


def _admitted_inspect_contract(contract: dict[str, Any]) -> bool:
    """J-Space admits inspect_shell only on a v2 read/command contract without write grants."""
    policy = contract.get("inspect_shell")
    operations = contract.get("allowed_operations")
    return (isinstance(policy, dict) and policy.get("schema_version") == "nokiy_inspect_shell_v1"
            and contract.get("schema_version") == "jspace_contract_v2"
            and isinstance(operations, list) and sorted(operations) == ["command", "read"]
            and contract.get("write_scopes") == [] and contract.get("declared_targets") in (None, []))


def _parent_shell_failure(item: dict[str, Any], output: dict[str, Any]) -> bool:
    """A parent-executed shell line (J-Space parent_shell) that exited non-zero and was fully settled.

    The parent ran it under the contract's bound read-only profile, so like an inspect shell the
    exit is a finding, not an unknown effect; it explains its own failed record only."""
    receipt, exit_code = output.get("terminal_receipt"), output.get("exit_code")
    return (item.get("command_type") in _INSPECT_SHELL_KINDS
            and not set(output) - {"exit_code", "message", "stderr", "stdout",
                                   "terminal_receipt", "terminal_receipt_path"}
            and type(exit_code) is int and exit_code != 0
            and isinstance(receipt, dict) and type(receipt.get("exit_code")) is int
            and receipt["exit_code"] == exit_code and receipt.get("pid") is None
            and all(receipt.get(key) == value and type(receipt.get(key)) is type(value)
                    for key, value in {**_CLOSED_SHELL_FAILURE_RECEIPT,
                                       "termination_origin": "parent_shell"}.items())
            and all(receipt.get(key) in (None, False) for key in ("timed_out", "cancelled"))
            and receipt.get("error") in (None, "")
            and all(receipt.get(key) in (None, []) for key in ("unresolved_effects", "apply_patch_changes")))


def _admitted_parent_shell_contract(contract: dict[str, Any]) -> bool:
    """J-Space admits parent_shell with read and command operations and without an inspect shell."""
    policy = contract.get("parent_shell")
    operations = contract.get("allowed_operations")
    return (isinstance(policy, dict) and policy.get("schema_version") == "nokiy_parent_shell_v1"
            and isinstance(operations, list) and {"read", "command"}.issubset(operations)
            and "inspect_shell" not in contract)


def _source_read_without_content(item: dict[str, Any], command: Any, output: dict[str, Any]) -> str | None:
    """The path of an in-process read that failed before returning any content.

    A page out of range, an over-long line, a missing JSON pointer, an oversized value or a
    mismatched digest all end in the runtime's effect-free read receipt. Off the writable paths
    such a failure explains its own record; nothing about the source is inferred from it."""
    receipt, error = output.get("terminal_receipt"), output.get("error_type")
    if (item.get("command_type") != "source_read" or not isinstance(command, str)
            # Diagnostics such as json_recovery may vary; effect-bearing fields may not appear.
            or set(output) & {"changes", "results", "pre_execution_rejection", "file_changes"}
            or not isinstance(error, str) or not error.startswith("SOURCE_READ_")
            or output.get("stderr") != error or output.get("stdout") != ""
            or not isinstance(receipt, dict)
            or any(receipt.get(key) != value or (value is not None and type(receipt.get(key)) is not type(value))
                   for key, value in _EFFECT_FREE_READ_RECEIPT.items())):
        return None
    args, rejection = _source_read_arguments(command)
    if args is None or rejection is not None or not isinstance(args.get("path"), str):
        return None
    # Facts the receipt binds must be the ones shown, and must describe this exact read.
    if any(key in receipt and output.get(key) != receipt[key]
           for key in ("navigation_refusal", "source_read_evidence")):
        return None
    evidence = receipt.get("source_read_evidence")
    if evidence is not None:
        request = evidence.get("request") if isinstance(evidence, dict) else None
        if (not isinstance(request, dict) or evidence.get("content_satisfied") is not False
                or request.get("path") != args["path"]
                or any(args[key] != request.get(key) for key in ("start_line", "end_line") if key in args)):
            return None
    return args["path"]


def _checked_results(run: Path, output: dict[str, Any],
                     payload_receipts: list[str] | None = None, *,
                     item: dict[str, Any] | None = None, event_index: int | None = None) -> list[str]:
    """Check every batch result, including receipts of commands we will retain."""
    if "results" not in output:
        return []
    entries = output["results"]
    if not isinstance(entries, list) or not entries:
        return ["unavailable"]
    proofs = []
    for result_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            proofs.append("unavailable")
            if payload_receipts is not None:
                payload_receipts.append("unavailable")
            continue
        identity = {**({"runtime_id": item["runtime_id"]} if isinstance(item, dict) and "runtime_id" in item else {}),
                    **entry}
        entry_receipt = _receipt(run, entry, item=identity, event_index=event_index, result_index=result_index)
        payload = entry.get("output")
        if not isinstance(payload, dict):
            proofs.append("unavailable")
            if payload_receipts is not None:
                payload_receipts.append("unavailable")
            continue
        payload_receipt = _receipt(run, payload, item=identity, event_index=event_index, result_index=result_index)
        if payload_receipts is not None:
            payload_receipts.append(payload_receipt)
        if (entry_receipt == payload_receipt == "matched"
                and entry["terminal_receipt"] != payload["terminal_receipt"]):
            raise InspectionError("RECEIPT_MISMATCH")
        codes = [value["exit_code"] for value in (entry, payload)
                 if value.get("exit_code") is not None]
        for value, state in ((entry, entry_receipt), (payload, payload_receipt)):
            if state == "matched":
                codes.append(value["terminal_receipt"]["exit_code"])
        if any(type(code) is not int for code in codes):
            raise InspectionError("EXIT_CODE_INVALID")
        if len(set(codes)) > 1:
            raise InspectionError("EXIT_CODE_CONFLICT")
        code = codes[0] if codes else None
        failed = any(value.get("success") is False or value.get("isError") is True
                     or value.get("status") == "failed" for value in (entry, payload))
        proofs.append("known_failure" if failed or (code is not None and code != 0) else
                      "known_success" if code == 0 and entry.get("success") is True
                      and all(_settled_output(value) for value in (entry, payload))
                      and "matched" in (entry_receipt, payload_receipt) else "unavailable")
    return proofs


def _typed_parts(item: dict[str, Any], output: dict[str, Any],
                 receipt_state: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Use declared types, not stdout markers or substrings of shell commands."""
    kind = item.get("command_type")
    if "results" not in output:
        # Current runtime observations omit command_type; their matched durable
        # receipt identifies the in-process executor without guessing from JSON.
        if kind is None and receipt_state == "matched":
            origin = output["terminal_receipt"].get("termination_origin")
            if origin == "in_process_source_read":
                kind = "source_read"
            elif origin == "parent_verifier" and output.get("executor") == "parent_focused_verifier":
                kind = "focused_verifier"
        return [(kind, item["command"], output)] if (
            isinstance(kind, str) and output.get("command_type", kind) == kind) else []
    if kind != "command_run":
        return []
    try:
        declared = _json(item["command"].encode()).get("commands")
    except InspectionError:
        return []
    entries = output["results"]
    if (not isinstance(declared, list) or not declared or not isinstance(entries, list)
            or len(declared) != len(entries)):
        return []
    parts = []
    for command, entry in zip(declared, entries):
        if not isinstance(command, dict) or not isinstance(entry, dict):
            return []
        kind, line = command.get("command_type"), command.get("command_line")
        payload = entry.get("output")
        if (not isinstance(kind, str) or not isinstance(line, str)
                or entry.get("command_type") != kind or not isinstance(payload, dict)
                or entry.get("command_line", line) != line
                or payload.get("command_type", kind) != kind):
            return []
        parts.append((kind, line, payload))
    return parts


def _normalized_source_read(line: str) -> dict[str, Any] | None:
    """The source_read selector contract, including its positional range form."""
    if len(line.encode("utf-8")) > 16384:
        return None
    try:
        args = json.loads(line, object_pairs_hook=caller._unique_object,
                          parse_constant=caller._invalid_constant)
    except (ValueError, RecursionError):
        return None
    fields = ("path", "start_line", "end_line", "expected_sha256", "line_numbers", "search_terms", "context_lines")
    if isinstance(args, list):
        if not 3 <= len(args) <= 7:
            return None
        args = dict(zip(fields, args))
    if not isinstance(args, dict) or set(args) - set(fields) - {"json_pointer", "children_offset"}:
        return None
    if "json_pointer" in args and not isinstance(args["json_pointer"], str):
        return None
    if "children_offset" in args and (type(args["children_offset"]) is not int
                                      or not 0 <= args["children_offset"] < 2**64):
        return None
    args = {key: value for key, value in args.items() if value is not None}
    path, pin = args.get("path"), args.get("expected_sha256")
    if (not isinstance(path, str) or not path or len(path.encode()) > 4096 or "\\" in path or "\0" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
            or not isinstance(pin, str) or re.fullmatch(r"[0-9a-fA-F]{64}", pin) is None
            or any(type(args[key]) is not int or not 0 <= args[key] < 2**64
                   for key in ("start_line", "end_line", "context_lines") if key in args)
            or ("line_numbers" in args and type(args["line_numbers"]) is not bool)):
        return None
    args["expected_sha256"] = pin.lower()
    if "json_pointer" in args:
        pointer = args["json_pointer"]
        if (set(args) - {"path", "expected_sha256", "json_pointer", "children_offset"}
                or pointer and not pointer.startswith("/") or re.search(r"~(?:[^01]|$)", pointer)):
            return None
    elif "children_offset" in args:
        return None
    elif "search_terms" in args:
        terms = args["search_terms"]
        if ("end_line" in args or args.get("line_numbers", True) is not True
                or not isinstance(terms, list) or not 1 <= len(terms) <= 16
                or any(not isinstance(term, str) or not term for term in terms)
                or sum(len(term.encode()) for term in terms) > 2048
                or args.get("start_line", 1) == 0 or args.get("context_lines", 0) > 5):
            return None
        args.update(start_line=args.get("start_line", 1), context_lines=args.get("context_lines", 0), line_numbers=True)
    else:
        if ("context_lines" in args or not 0 < args.get("start_line", 0) <= args.get("end_line", 0)):
            return None
        args["line_numbers"] = args.get("line_numbers", True)
    return args


def _own_edit_receipt_settled(payload: dict[str, Any], origin: str, failed: bool) -> bool:
    receipt = payload.get("terminal_receipt", {})
    code, state = (1, "failed") if failed else (0, "completed")
    return (receipt.get("termination_origin") == origin and receipt.get("pid") is None
            and type(receipt.get("exit_code")) is int and receipt["exit_code"] == code
            and receipt.get("failure_class") == ("workload_exit_nonzero" if failed else "none")
            and receipt.get("reconcile_required") is failed
            and all(receipt.get(key) is False for key in ("retry_safe", "auto_retry_allowed"))
            and receipt.get("authority_effect") == receipt.get("staging_authority") == "none"
            and receipt.get("authoritative_publication") == "unproven"
            and all(value.get("outcome", "known") == "known"
                    and value.get("terminal_state", state) == state
                    and value.get("status", state) == state
                    and all(value.get(key, True) is True for key in
                            ("process_reaped", "process_group_empty", "termination_proven"))
                    and all(value.get(key, False) is False for key in ("timed_out", "cancelled"))
                    and value.get("isError", False) is False
                    and value.get("reconcile_required", failed) is failed
                    and value.get("unresolved_effects") in (None, []) and value.get("error") in (None, "")
                    for value in (receipt, payload))
            and payload.get("success", not failed) is (not failed)
            and type(payload.get("exit_code")) is int and payload["exit_code"] == code)


def _source_read_content(line: str, payload: dict[str, Any], failed: bool) -> dict[str, Any] | None:
    proof = payload.get("source_read_evidence")
    request = _normalized_source_read(line)
    if (not isinstance(proof, dict) or set(proof) != {"schema_version", "request", "source_sha256", "content_satisfied"}
            or len(_compact_utf8_json(proof)) > 4096 or request is None
            or proof["schema_version"] != "nokiy_source_read_content_v1"
            or _compact_utf8_json(proof["request"]) != _compact_utf8_json(request)
            or not file_change_evidence._sha(proof["source_sha256"])
            or proof["content_satisfied"] is not (not failed)
            or proof["content_satisfied"] is not (request["expected_sha256"] == proof["source_sha256"])
            or _compact_utf8_json(payload["terminal_receipt"].get("source_read_evidence")) != _compact_utf8_json(proof)
            or not _own_edit_receipt_settled(payload, "in_process_source_read", failed)
            or payload.get("changes") not in (None, [])
            or payload.get("failed_change") is not None or payload.get("jspace_error_code") is not None
            or (failed and (payload.get("error_type") != "SOURCE_READ_SHA256_MISMATCH"
                            or payload.get("stdout") != "" or payload.get("stderr") != "SOURCE_READ_SHA256_MISMATCH"))
            or (not failed and (payload.get("truncated") is not False or payload.get("path") != request["path"]
                                or payload.get("source_sha256") != proof["source_sha256"]))):
        return None
    return proof


def _source_read_json_oversize(line: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Bind the observed object/array recovery hint, never the failed value body."""
    request = _normalized_source_read(line)
    recovery = payload.get("json_recovery")
    error = "SOURCE_READ_JSON_VALUE_TOO_LARGE"
    if (request is None or set(request) != {"path", "expected_sha256", "json_pointer"}
            or not _own_edit_receipt_settled(payload, "in_process_source_read", True)
            or "source_read_evidence" in payload or "source_read_evidence" in payload["terminal_receipt"]
            or payload.get("error_type") != error or payload.get("stderr") != error
            or payload.get("stdout") != "" or payload.get("changes") not in (None, [])
            or payload.get("failed_change") is not None or payload.get("jspace_error_code") is not None
            or not isinstance(recovery, dict) or set(recovery) != {
                "child_count", "children", "children_page", "incomplete", "parent_pointer",
                "parent_pointer_omitted", "parent_type", "source_sha256"}
            or len(_compact_utf8_json(recovery)) > 4096
            or recovery["source_sha256"] != request["expected_sha256"]
            or payload.get("source_sha256", recovery["source_sha256"]) != recovery["source_sha256"]
            or payload.get("path", request["path"]) != request["path"]
            or recovery["parent_pointer"] != request["json_pointer"]
            or recovery["parent_pointer_omitted"] is not False
            or recovery["parent_type"] not in ("object", "array")
            or type(recovery["child_count"]) is not int or not 0 < recovery["child_count"] < 2**64
            or not isinstance(recovery["children"], list)
            or not 0 < len(recovery["children"]) <= min(recovery["child_count"], 128)
            or recovery["incomplete"] is not (len(recovery["children"]) < recovery["child_count"])
            or _compact_utf8_json(recovery["children_page"]) != _compact_utf8_json(
                {"json_pointer": request["json_pointer"], "children_offset": 0})):
        return None
    token = "key" if recovery["parent_type"] == "object" else "index"
    seen = set()
    for child in recovery["children"]:
        if not isinstance(child, dict) or set(child) != {token, "json_pointer", "type"}:
            return None
        value = child[token]
        if ((token == "key" and not isinstance(value, str))
                or (token == "index" and (type(value) is not int or not 0 <= value < recovery["child_count"]))
                or child["type"] not in ("object", "array", "string", "number", "boolean", "null")):
            return None
        pointer = request["json_pointer"] + "/" + str(value).replace("~", "~0").replace("/", "~1")
        if value in seen or child["json_pointer"] != pointer:
            return None
        seen.add(value)
    return {"request": request, "source_sha256": recovery["source_sha256"], "json_recovery": recovery}


def _source_read_json_route(content: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    """Validate only the returned projection/page; summaries do not cover child values."""
    request = content["request"]
    text = payload.get("stdout")
    if ("json_pointer" not in request or payload.get("json_pointer") != request["json_pointer"]
            or payload.get("stderr") != "" or payload.get("error_type") is not None
            or not isinstance(text, str) or not text or len(text.encode("utf-8")) > 10000):
        return None
    if "children_offset" not in request:
        if payload.get("mode") != "json_projection":
            return None
        try:
            json.loads(text, object_pairs_hook=caller._unique_object, parse_constant=caller._invalid_constant)
        except (ValueError, RecursionError, caller.EmbeddedNokiyError):
            return None
        return {"mode": "json_projection"}
    counts = ("child_count", "child_inline_bytes", "children_offset", "children_returned", "children_summarized")
    if (payload.get("mode") != "json_children" or payload.get("parent_type") not in ("object", "array")
            or any(type(payload.get(key)) is not int or not 0 <= payload[key] < 2**64 for key in counts)
            or not {"children_at_end", "children_next_offset"} <= payload.keys()
            or payload["children_offset"] != request["children_offset"] or payload["child_inline_bytes"] != 1024
            or not 0 < payload["children_returned"] <= payload["child_count"] - payload["children_offset"]
            or payload["children_summarized"] > payload["children_returned"]):
        return None
    end = payload["children_offset"] + payload["children_returned"]
    if (payload["children_at_end"] is not (end == payload["child_count"])
            or _compact_utf8_json(payload["children_next_offset"]) != _compact_utf8_json(
                None if end == payload["child_count"] else end)):
        return None
    try:
        rows = [_json(row.encode("utf-8")) for row in text.splitlines()]
    except InspectionError:
        return None
    token = "key" if payload["parent_type"] == "object" else "index"
    seen, summarized = set(), 0
    for offset, row in enumerate(rows, payload["children_offset"]):
        value = row.get(token)
        if ((token == "key" and not isinstance(value, str))
                or (token == "index" and (type(value) is not int or value != offset)) or value in seen):
            return None
        seen.add(value)
        if set(row) == {token, "value"}:
            continue
        if (set(row) - {token, "json_pointer", "type", "bytes", "child_count"}
                or row.get("json_pointer") != request["json_pointer"] + "/" + str(value).replace("~", "~0").replace("/", "~1")
                or row.get("type") not in ("object", "array", "string", "number", "boolean", "null")
                or type(row.get("bytes")) is not int or row["bytes"] <= payload["child_inline_bytes"]
                or (row["type"] in ("object", "array") and
                    (type(row.get("child_count")) is not int or not 0 <= row["child_count"] < 2**64))
                or (row["type"] not in ("object", "array") and "child_count" in row)):
            return None
        summarized += 1
    if len(rows) != payload["children_returned"] or summarized != payload["children_summarized"]:
        return None
    return {"mode": "json_children", "parent_type": payload["parent_type"], "child_count": payload["child_count"]}


def _own_edit_position(meta: dict[str, Any], payload: dict[str, Any]) -> tuple[str, int, int] | None:
    call = payload.get("terminal_receipt", {}).get("call_id")
    step, index = meta.get("step"), meta.get("command_index")
    if (not isinstance(call, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", call) is None
            or type(step) is not int or not 0 < step < 2**64
            or any(meta.get(key) is not None for key in ("workdir", "cwd", "arguments", "jspace_error_code"))):
        return None
    runtime, sep, tail = call.partition(".tool.command_run:")
    if not runtime or not sep or meta.get("runtime_id") != runtime:
        return None
    provider, identity = meta.get("provider_tool_call_id"), meta.get("command_id")
    if provider is not None or identity is not None:
        if (not isinstance(provider, str) or type(index) is not int or not 0 <= index < 512
                or tail != f"{provider}:{index}"):
            return None
        # Runtime results use relative IDs; gateway events qualify them with
        # the runtime. Accept either complete pair, never a mixture of both.
        if (identity, meta.get("command_run_id", provider)) not in (
                (tail, provider), (call, f"{runtime}.tool.command_run")):
            return None
        return runtime + sep + provider, step, index
    match = re.fullmatch(r"(.+):step:([0-9]+):index:([0-9]+)", tail)
    if match is None or int(match[2]) != step or int(match[3]) >= 512:
        return None
    if index is not None and (type(index) is not int or index != int(match[3])):
        return None
    return runtime + sep + match[1], step, int(match[3])


def _own_edit_observation_settled(value: dict[str, Any], failed: bool) -> bool:
    state = "failed" if failed else "completed"
    code = value.get("exit_code")
    return (value.get("outcome", "known") == "known" and value.get("status", state) == state
            and value.get("terminal_state", state) == state and value.get("success", not failed) is (not failed)
            and value.get("isError", False) is False
            and (code is None or type(code) is int and code == int(failed))
            and all(value.get(key, True) is True for key in ("process_reaped", "process_group_empty", "termination_proven"))
            and all(value.get(key, False) is False for key in ("timed_out", "cancelled"))
            and value.get("unresolved_effects") in (None, [])
            and value.get("error") in ((None, "", "SOURCE_READ_SHA256_MISMATCH") if failed else (None, "")))


def _resolve_own_edit_reads(run: Path, events: list[dict[str, Any]], checked: dict[int, Any],
                            original: dict[str, Any], contract: dict[str, Any]) -> tuple[dict[int, Any], int]:
    """Reconcile stale own-edit pins and observed JSON routes, never rewrite history."""
    scopes = [contract.get(key, ()) for key in ("read_scopes", "write_scopes", "declared_targets")]
    if (contract.get("repo_root") != original.get("workspace") or contract.get("source_read") is not True
            or not file_change_evidence._sha(contract.get("authorization_semantic_sha256"))
            or not {"command", "read"} <= set(contract.get("allowed_operations", ()))
            or {"command", "read"} & set(contract.get("denied_operations", ()))):
        return {}, 0
    own_edits_allowed = ("modify" in contract.get("allowed_operations", ())
                         and "modify" not in contract.get("denied_operations", ()))
    groups, calls, eligible, failed_count = [], {}, {}, 0
    closed = set()
    for event_index, event in enumerate(events):
        item = event.get("item", {})
        if event.get("type") != "item.completed" or item.get("type") not in ("command_execution", "file_change"):
            continue
        output = full_core._structured_output(item)
        for call in _command_call_ids(item, output):
            calls[call] = calls.get(call, 0) + 1
        parts, proofs, receipts = checked.get(event_index, ([], [], []))
        if item["type"] == "file_change":
            parts, proofs, receipts = [("apply_patch", "", output)], ["known_success"], [
                _receipt(run, output, item=item, event_index=event_index)]
        if not parts:
            # Native status-only bookkeeping is not an effect or a repair proof.
            if item.get("command_type") == "task_status" and item.get("status") == "completed" and set(output) == {"task_status"}:
                continue
            groups.append((None, []))
            continue
        eligible[event_index] = (len(parts) == len(proofs) == len(receipts)
                                 and _own_edit_observation_settled(item, "known_failure" in proofs)
                                 and _own_edit_observation_settled(output, "known_failure" in proofs))
        event_settled = eligible[event_index]
        declared = _json(item["command"].encode()).get("commands") if "results" in output else None
        part_calls = set()
        for part_index, ((kind, line, payload), proof, receipt_check) in enumerate(zip(parts, proofs, receipts)):
            meta = item if declared is None else {**declared[part_index], **output["results"][part_index]}
            position = _own_edit_position(meta, payload) if event_settled and receipt_check == "matched" else None
            if not _own_edit_observation_settled(meta, proof == "known_failure"):
                position = None
            if declared is not None:
                entry, command = output["results"][part_index], declared[part_index]
                if (meta.get("command_index", part_index) != part_index
                        or any(key in entry and key in command and _compact_utf8_json(entry[key]) != _compact_utf8_json(command[key])
                               for key in ("step", "command_index", "command_id", "command_run_id", "provider_tool_call_id", "runtime_id"))):
                    position = None
            call = payload.get("terminal_receipt", {}).get("call_id")
            if isinstance(call, str):
                if call in part_calls:
                    calls[call] += 1  # Reuse inside one batch is not a duplicate reporting copy.
                part_calls.add(call)
            failed = proof == "known_failure"
            failed_count += kind == "source_read" and failed
            if proof != "known_success" and not (kind == "source_read" and failed):
                eligible[event_index] = False
            prefix = position[0] if position else None
            row = (event_index, kind, line, payload, proof, position, call)
            if groups and prefix is not None and groups[-1][0] == prefix:
                groups[-1][1].append(row)
            else:
                if prefix in closed:
                    return {}, failed_count  # Crossed/interleaved batch identities are ambiguous.
                if groups and groups[-1][0] is not None:
                    closed.add(groups[-1][0])
                groups.append((prefix, [row]))
    patches, failures, generation = {}, [], 0
    for batch_index, (prefix, rows) in enumerate(groups):
        if prefix is None:
            patches.clear()
            generation += 1
            continue
        if len({row[5][2] for row in rows}) != len(rows):
            return {}, failed_count
        mutations = [row[5][1] for row in rows if row[1] not in ("source_read", "focused_verifier", "task_status", "planning")]
        for event_index, kind, line, payload, proof, position, call in sorted(rows, key=lambda row: (row[5][1], row[5][2])):
            at = (batch_index, position[1])
            if calls.get(call) != 1:
                eligible[event_index] = False
                patches.clear()
                generation += 1
                continue
            if kind not in ("source_read", "focused_verifier", "task_status", "planning"):
                patches.clear()
                generation += 1
                facts = payload.get("terminal_receipt", {}).get("apply_patch_changes")
                changes = payload.get("changes")
                if (not own_edits_allowed or kind != "apply_patch" or proof != "known_success" or mutations.count(position[1]) != 1
                        or not _own_edit_receipt_settled(payload, "in_process_apply_patch", False)
                        or payload.get("failed_change") is not None or not isinstance(facts, list)
                        or not 0 < len(facts) <= 128 or len(_compact_utf8_json(facts)) > 16384
                        or not isinstance(changes, list) or len(changes) != len(facts)):
                    continue
                valid = all(isinstance(fact, dict) and set(fact) == {"kind", "path", "preimage_sha256", "postimage_sha256"}
                            and fact["kind"] == "update" and all(fact["path"] in scope for scope in scopes)
                            and all(file_change_evidence._sha(fact[key]) for key in ("preimage_sha256", "postimage_sha256"))
                            and _normalized_source_read(json.dumps({"path": fact["path"], "start_line": 1, "end_line": 1,
                                                                  "expected_sha256": fact["postimage_sha256"]})) is not None
                            and isinstance(change, dict) and change.get("move_path") is None
                            and _compact_utf8_json({key: change.get(key) for key in fact}) == _compact_utf8_json(fact)
                            for fact, change in zip(facts, changes))
                if valid and len({fact["path"] for fact in facts}) == len(facts):
                    patches = {fact["path"]: (at, fact, call) for fact in facts}
            elif kind == "source_read" and position[1] not in mutations:
                oversized = _source_read_json_oversize(line, payload) if proof == "known_failure" else None
                if oversized is not None and oversized["request"]["path"] in scopes[0]:
                    failures.append([event_index, oversized, generation, at, call, None, None])
                    continue
                content = _source_read_content(line, payload, proof == "known_failure")
                if content is None or content["request"]["path"] not in scopes[0]:
                    if proof != "known_success":
                        eligible[event_index] = False
                    continue
                if proof == "known_failure":
                    patch = patches.get(content["request"]["path"])
                    if (patch is None or patch[0] >= at or patch[1]["preimage_sha256"] != content["request"]["expected_sha256"]
                            or patch[1]["postimage_sha256"] != content["source_sha256"]):
                        eligible[event_index] = False
                        continue
                    failures.append([event_index, content, generation, at, call, patch[2], None])
                elif proof == "known_success":
                    selector = {key: value for key, value in content["request"].items() if key != "expected_sha256"}
                    route = _source_read_json_route(content, payload)
                    for failure in failures:
                        if (failure[2] != generation or failure[3] >= at
                                or content["source_sha256"] != failure[1]["source_sha256"]):
                            continue
                        if failure[5] is None:
                            parent = failure[1]["request"]["json_pointer"]
                            pointer = content["request"].get("json_pointer")
                            same_parent = pointer == parent and route is not None and route["mode"] == "json_children"
                            if (failure[6] is None and route is not None and isinstance(pointer, str)
                                    and content["request"]["path"] == failure[1]["request"]["path"]
                                    and (pointer.startswith(parent + "/") or same_parent
                                         and route["parent_type"] == failure[1]["json_recovery"]["parent_type"]
                                         and route["child_count"] == failure[1]["json_recovery"]["child_count"])):
                                failure[6] = {"passing_event_index": event_index, "failed_call_id": failure[4],
                                              "passing_call_id": call, "path": content["request"]["path"],
                                              "source_sha256": content["source_sha256"],
                                              "failure_kind": "SOURCE_READ_JSON_VALUE_TOO_LARGE",
                                              "recovery_kind": "json_read_route", "mode": route["mode"],
                                              "observed_request": content["request"],
                                              "original_request_satisfied": False, "original_value_coverage": "unproven"}
                            continue
                        old_selector = {key: value for key, value in failure[1]["request"].items() if key != "expected_sha256"}
                        if selector == old_selector:
                            failure[6] = {"passing_event_index": event_index, "failed_call_id": failure[4],
                                          "passing_call_id": call, "patch_call_id": failure[5],
                                          "path": content["request"]["path"], "postimage_sha256": content["source_sha256"]}
            elif kind == "source_read" and proof != "known_success":
                eligible[event_index] = False
    resolved = {}
    for event_index in {row[0] for row in failures}:
        links = [row[6] for row in failures if row[0] == event_index]
        if eligible.get(event_index) and links and all(link is not None for link in links):
            resolved[event_index] = {"status": "resolved", "source_reads": links}
    return resolved, failed_count


def _parent_verifier_observation(line: str, payload: dict[str, Any],
                                 receipt_state: str) -> dict[str, Any] | None:
    """Only an authenticated workload observation, never a tool/transport failure."""
    if receipt_state != "matched":
        return None
    receipt = payload.get("terminal_receipt", {})
    code = payload.get("exit_code")
    call_id = receipt.get("call_id")
    if (receipt.get("termination_origin") != "parent_verifier"
            or payload.get("executor") != "parent_focused_verifier"
            or (code != 0 and receipt.get("failure_class") != "workload")
            or (code == 0 and receipt.get("failure_class") not in (None, "none", "workload"))
            or type(code) is not int or code < 0 or code != receipt.get("exit_code")
            or payload.get("success", code == 0) is not (code == 0)
            or payload.get("outcome", "known") != "known"
            or payload.get("process_reaped", True) is not True
            or payload.get("process_group_empty", True) is not True
            or payload.get("isError", False) is not False or payload.get("error") is not None
            or payload.get("status") not in (None, "completed" if code == 0 else "failed")
            or payload.get("terminal_state") not in (None, "completed" if code == 0 else "failed")
            or any(payload.get(key) not in (None, False) for key in ("cancelled", "timed_out", "timeout"))
            or not isinstance(call_id, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", call_id) is None):
        return None
    try:
        args = _json(line.encode())
    except InspectionError:
        return None
    index = args.get("verifier_index")
    if set(args) != {"verifier_index"} or type(index) is not int or index < 0:
        return None
    return {"verifier_index": index, "call_id": call_id, "exit_code": code,
            "verification_evidence": payload.get("verification_evidence")}


def _inspection_contract(run: Path, terminal: dict[str, Any], original: dict[str, Any],
                         artifacts: dict[str, Any], *, require_artifact: bool) -> dict[str, Any]:
    reference = original.get("jspace_contract")
    artifact = terminal.get("original_jspace_artifact")
    if require_artifact or artifact is not None:
        raw, artifacts["original_jspace"] = _artifact(run, "jspace-original.json", artifact, MAX_JSON)
    else:
        raw = _file(run / "jspace-original.json", MAX_JSON)
        artifacts["original_jspace"] = {"path": "jspace-original.json",
                                        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    if (not isinstance(reference, dict) or not file_change_evidence._sha(reference.get("sha256"))
            or hashlib.sha256(raw).hexdigest() != reference["sha256"]):
        raise InspectionError("FILE_CHANGE_CONTRACT_HASH_MISMATCH")
    contract = _json(raw)
    if caller._canonical_bytes(contract) != caller._canonical_bytes(_json(_file(run / "jspace.json", MAX_JSON))):
        raise InspectionError("FILE_CHANGE_CONTRACT_MISMATCH")
    return contract


def _resolution_sources(original: dict[str, Any], contract: dict[str, Any],
                        postimages: list[dict[str, Any]] | None) -> dict[str, dict | None] | None:
    """Use only request-bound initial identities and verified closeout postimages."""
    digest = caller._canonical_sha256({k: v for k, v in original.items()
                                      if k not in ("request_id", "request_sha256")})
    workspace = original.get("workspace")
    if (not isinstance(workspace, str) or original.get("request_sha256") != digest
            or original.get("request_id") != "tura_embedded_" + digest
            or contract.get("repo_root") != workspace
            or not file_change_evidence._sha(contract.get("authorization_semantic_sha256"))):
        return None
    names = _verification_targets(Path(workspace), contract)
    if names is None:
        return None
    final = {row["path"]: {k: row[k] for k in ("sha256", "bytes", "mode")}
             for row in postimages or ()}
    if any(name not in names for name in final):
        return None
    generation = contract.get("dcf_generation")
    initial = generation.get("source_snapshot", {}) if isinstance(generation, dict) else {}
    for name in names:
        if name in final:
            continue
        if _admitted_absent_creation(contract, name):
            final[name] = None
            continue
        row = initial.get(name) if isinstance(initial, dict) else None
        if (not isinstance(row, dict) or not file_change_evidence._sha(row.get("sha256"))
                or type(row.get("mode")) is not int or not 0 <= row["mode"] <= 0o7777
                or ("bytes" in row and (type(row["bytes"]) is not int
                    or not 0 <= row["bytes"] <= file_change_evidence.MAX_FILE_BYTES))):
            return None
        final[name] = {k: row[k] for k in ("sha256", "bytes", "mode") if k in row}
    return final


def _verification_matches(observation: dict[str, Any], contract: dict[str, Any],
                          sources: dict[str, dict | None]) -> bool:
    value = observation["verification_evidence"]
    grants = contract.get("verifier_commands")
    index = observation["verifier_index"]
    if (not isinstance(grants, list) or not 0 <= index < len(grants)
            or not isinstance(grants[index], dict) or not isinstance(value, dict)
            or set(value) != {"schema_version", "authorization_semantic_sha256", "verifier_index",
                              "verifier_sha256", "call_id", "source_postimages"}
            or value["schema_version"] != VERIFICATION_EVIDENCE_SCHEMA
            or value["authorization_semantic_sha256"] != contract["authorization_semantic_sha256"]
            or type(value["verifier_index"]) is not int or value["verifier_index"] != index
            or value["verifier_sha256"] != caller._canonical_sha256(grants[index])
            or value["call_id"] != observation["call_id"]):
        return False
    postimages = value["source_postimages"]
    return (isinstance(postimages, dict) and set(postimages) == set(sources)
            and all((row is None if sources[name] is None else
                     isinstance(row, dict) and set(row) == {"sha256", "bytes", "mode"}
                    and file_change_evidence._sha(row["sha256"])
                    and type(row["bytes"]) is int and 0 <= row["bytes"] <= file_change_evidence.MAX_FILE_BYTES
                    and type(row["mode"]) is int and 0 <= row["mode"] <= 0o7777
                    and all(row[key] == expected for key, expected in sources[name].items()))
                    for name, row in postimages.items()))


def _read_only(parts: list[tuple[str, str, dict[str, Any]]], proof: str) -> bool:
    if proof != "known_success" or not parts:
        return False
    for kind, line, payload in parts:
        path, sha = payload.get("path"), payload.get("source_sha256")
        if (kind != "source_read" or not isinstance(path, str) or not path
                or not isinstance(sha, str) or _SHA.fullmatch(sha) is None
                or _read_lines(payload) is None or payload.get("error")
                or any(key in payload for key in ("results", "changes", "file_changes", "postimage",
                                                 "postimages", "source_postimages"))):
            return False
        # Native per-command observations may use the exact type name. Otherwise
        # the declared read arguments must agree with the returned path.
        if line != "source_read":
            try:
                arguments = _json(line.encode())
            except InspectionError:
                return False
            if (arguments.get("path") != path or "commands" in arguments
                    or ("expected_sha256" in arguments and arguments["expected_sha256"] != sha)):
                return False
    return True


class _ParentReviewContext:
    """Optional recorded reads and verifier context, never authority or live proof.

    Absence means no context was delivered (including missing, invalid or oversized
    observations). Unlisted lines/files/dependencies remain omitted even when every
    retained range fits. All inputs come from the existing verified inspection pass.
    """
    def __init__(self, contract: dict[str, Any], postimages: list[dict[str, Any]],
                 request: dict[str, Any], mutation_indices: dict[str, int]) -> None:
        self.contract, self.postimages, self.request = contract, postimages, request
        self.mutation_indices = mutation_indices
        self.raw_blocked = False
        self.sources: dict[str, dict] | None = None
        scopes = contract.get("read_scopes")
        allowed, denied = contract.get("allowed_operations"), contract.get("denied_operations", [])
        admitted = (contract.get("source_read") is True
                    and isinstance(scopes, list) and all(isinstance(p, str) for p in scopes)
                    and isinstance(allowed, list) and {"read", "command"}.issubset(allowed)
                    and isinstance(denied, list) and not {"read", "command"}.intersection(denied))
        self.final = {p["path"]: p["sha256"] for p in postimages
                      if admitted and p["path"] in scopes}
        self.reset()

    def reset(self) -> None:
        # A later typed mutation invalidates earlier observations, even at the same SHA.
        self.reset_reads()
        self.verifiers: list[dict[str, Any]] = []
        self.verifier_files: dict[str, tuple[str, dict, int]] = {}
        self.verifier_seen: dict[tuple[str, int], str] = {}
        self.verifier_calls: set[str] = set()
        self.verifier_bytes = 0
        self.invalid = False

    def reset_reads(self) -> None:
        # Raw-only effect barriers must not change verifier or conflict rules.
        self.ranges: dict[tuple[str, str, int, int], dict[str, Any]] = {}
        self.seen: dict[tuple[str, int], str] = {}
        self.totals: dict[str, int] = {}
        self.bytes = 0

    @staticmethod
    def _body(text: object, count: int) -> list[str]:
        if (not isinstance(text, str) or "\x00" in text
                or len(text) > postimage_context.MAX_JSON_BYTES
                or len(text.encode("utf-8")) > postimage_context.MAX_JSON_BYTES):
            raise ValueError("invalid context body")
        lines = postimage_context._lines(text)
        if len(lines) != count:
            raise ValueError("physical line count mismatch")
        return lines

    def _verifier_file(self, row: dict, schema: str) -> tuple[str, int]:
        definition = schema == postimage_context.SCHEMA_VERSION
        navigation = schema == postimage_context.DELTA_NAVIGATION_SCHEMA_VERSION
        keys = {"path", "preimage_sha256", "postimage_sha256"}
        keys |= {"locators", "spans"} if definition else {"coverage", "hunks"}
        if navigation:
            keys.add("locators")
        if not isinstance(row, dict) or set(row) != keys:
            raise ValueError("invalid context file")
        path, before, after = row["path"], row["preimage_sha256"], row["postimage_sha256"]
        if (not isinstance(path, str) or not postimage_context._safe_path(path)
                or ((definition or navigation) and Path(path).suffix != ".py")
                or not file_change_evidence._sha(before) or not file_change_evidence._sha(after)
                or before == after or self.final.get(path) != after
                or path not in self.sources or self.sources[path]["sha256"] != after):
            raise ValueError("foreign context identity")
        generation = self.contract.get("dcf_generation")
        before_bytes = None
        if isinstance(generation, dict) and "source_snapshot" in generation:
            snapshot = generation["source_snapshot"]
            pin = snapshot.get(path) if isinstance(snapshot, dict) else None
            if not isinstance(pin, dict) or postimage_context._sha(pin.get("sha256")) != before:
                raise ValueError("foreign context preimage")
            before_bytes = pin.get("bytes")
        bodies = row["spans" if definition else "hunks"]
        if not isinstance(bodies, list) or not bodies or len(bodies) > postimage_context.MAX_LINES:
            raise ValueError("invalid context ranges")
        previous_before = previous_after = count = 0
        before_eof = after_eof = False
        for body in bodies:
            if not isinstance(body, dict):
                raise ValueError("invalid context range")
            if definition:
                if set(body) != {"start_line", "end_line", "text"}:
                    raise ValueError("invalid context span")
                start, end = body["start_line"], body["end_line"]
                if (type(start) is not int or type(end) is not int
                        or not previous_after < start <= end or after_eof
                        or end > self.sources[path]["bytes"]):
                    raise ValueError("invalid context span bounds")
                lines = self._body(body["text"], end - start + 1)
                previous_after = end
                count += len(lines)
            else:
                if (row["coverage"] != "all_text_changes" or set(body) != {
                        "before_start_line", "before_line_count", "after_start_line", "after_line_count",
                        "before_text", "after_text"}):
                    raise ValueError("invalid context hunk")
                b, bc, start, ac = (body[key] for key in (
                    "before_start_line", "before_line_count", "after_start_line", "after_line_count"))
                if (any(type(value) is not int for value in (b, bc, start, ac))
                        or min(b, start) < 1 or min(bc, ac) < 0 or not bc + ac
                        or b <= previous_before or start <= previous_after or before_eof or after_eof
                        or b - previous_before != start - previous_after
                        or start - 1 + ac > self.sources[path]["bytes"]
                        or (type(before_bytes) is int and b - 1 + bc > before_bytes)
                        or body["before_text"] == body["after_text"]):
                    raise ValueError("invalid context hunk bounds")
                old_lines = self._body(body["before_text"], bc)
                lines = self._body(body["after_text"], ac)
                previous_before, previous_after = b - 1 + bc, start - 1 + ac
                before_eof = bool(old_lines and not old_lines[-1].endswith(("\r", "\n")))
                count += bc + ac
            after_eof = bool(lines and not lines[-1].endswith(("\r", "\n")))
            if count > postimage_context.MAX_LINES:
                raise ValueError("context line budget")
            for number, text in enumerate(lines, start):
                key = (path, number)
                if any(key in seen and seen[key] != text for seen in (self.seen, self.verifier_seen)):
                    raise ValueError("conflicting postimage lines")
                self.verifier_seen[key] = text
        if definition or navigation:
            locators, names = row["locators"], set()
            if not isinstance(locators, list) or (definition and not locators):
                raise ValueError("invalid context locators")
            for locator in locators:
                keys = {"qualified_name", "kind", "line", "start_line", "end_line"}
                if definition:
                    keys.add("complete")
                if not isinstance(locator, dict) or set(locator) != keys:
                    raise ValueError("invalid context locator")
                name = locator["qualified_name"]
                start, line, end = (locator[key] for key in ("start_line", "line", "end_line"))
                if (not isinstance(name, str) or not all(name.split(".")) or name in names
                        or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name)
                        or len(name) > postimage_context.MAX_JSON_BYTES
                        or (navigation and len(name.encode("utf-8")) > postimage_context.MAX_DELTA_NAME_BYTES)
                        or locator["kind"] not in ("function", "async_function", "class", "binding")
                        or any(type(value) is not int for value in (start, line, end))
                        or not 1 <= start <= line <= end <= self.sources[path]["bytes"]
                        or (definition and (locator["complete"] is not True or not any(
                            span["start_line"] <= start and end <= span["end_line"] for span in bodies)))):
                    raise ValueError("invalid context locator bounds")
                name.encode("utf-8")
                names.add(name)
        return path, count

    def add_verifier(self, payload: dict, observation: dict, index: int, event_sha256: str,
                     command_sha256: str, part_index: int, last_mutation_index: int) -> None:
        value = payload.get("source_postimages")
        if self.invalid or value is None or index <= last_mutation_index:
            return
        try:
            if self.sources is None:
                self.sources = _resolution_sources(self.request, self.contract, self.postimages)
            # The caller has checked the persisted per-command receipt/proof.
            # Producer-only process flags are absent from normalized wire records.
            if (self.sources is None
                    or self.contract.get("source_read") is not True
                    or "modify" not in self.contract.get("allowed_operations", [])
                    or "modify" in self.contract.get("denied_operations", [])
                    or not _verification_matches(observation, self.contract, self.sources)
                    or not isinstance(value, dict) or set(value) != {
                        "schema_version", "kind", "jspace_semantic_sha256", "notice", "files"}
                    or value["jspace_semantic_sha256"] != self.contract["authorization_semantic_sha256"]):
                raise ValueError("unbound verifier context")
            schema = value["schema_version"]
            expected = {
                postimage_context.SCHEMA_VERSION: ("verifier_postimage_context", postimage_context.NOTICE),
                postimage_context.DELTA_SCHEMA_VERSION: ("verifier_postimage_delta", postimage_context.DELTA_NOTICE),
                postimage_context.DELTA_NAVIGATION_SCHEMA_VERSION: (
                    "verifier_postimage_delta", postimage_context.DELTA_NAVIGATION_NOTICE),
            }
            if (schema not in expected or (value["kind"], value["notice"]) != expected[schema]
                    or not isinstance(value["files"], list)
                    or not 1 <= len(value["files"]) <= postimage_context.MAX_FILES):
                raise ValueError("invalid verifier context schema")
            names, lines, locators = set(), 0, 0
            for row in value["files"]:
                path, count = self._verifier_file(row, schema)
                identity = (schema, row, count)
                if path in names or (path in self.verifier_files and self.verifier_files[path] != identity):
                    raise ValueError("conflicting verifier context files")
                names.add(path)
                lines += count
                locators += len(row.get("locators", []))
                self.verifier_files[path] = identity
            if (lines > postimage_context.MAX_LINES or len(self.verifier_files) > postimage_context.MAX_FILES
                    or sum(row[2] for row in self.verifier_files.values()) > postimage_context.MAX_LINES
                    or (schema == postimage_context.DELTA_NAVIGATION_SCHEMA_VERSION
                        and locators > postimage_context.MAX_DELTA_LOCATORS)
                    or len(caller._canonical_bytes(value)) > postimage_context.MAX_JSON_BYTES):
                raise ValueError("verifier context budget")
            self.verifier_calls.add(observation["call_id"])
            if any(row["source_postimages"] == value for row in self.verifiers):
                return  # Retain the original event for an identical complete envelope.
            row = {"source_postimages": value, "source_event": {
                "event_index": index, "event_sha256": event_sha256, "command_sha256": command_sha256,
                "part_index": part_index, "call_id": observation["call_id"],
                "verifier_index": observation["verifier_index"],
                "receipt_path": payload["terminal_receipt_path"],
                "receipt_sha256": caller._canonical_sha256(payload["terminal_receipt"]),
                "verification_evidence_sha256": caller._canonical_sha256(observation["verification_evidence"])}}
            self.verifier_bytes += len(caller._canonical_bytes(row))
            if self.bytes + self.verifier_bytes > MAX_PAGE_BYTES:
                raise ValueError("review context budget")
            self.verifiers.append(row)
        except (InspectionError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
            self.invalid = True  # Optional bodies can never change inspection facts.

    def add(self, parts: list[tuple[str, str, dict[str, Any]]], proof: str,
            index: int, event_sha256: str) -> None:
        if self.invalid or self.raw_blocked or proof != "known_success":
            return
        for part_index, part in enumerate(parts):
            payload = part[2]
            path = payload.get("path")
            if (isinstance(path, str) and path in self.mutation_indices
                    and index <= self.mutation_indices[path]):
                continue
            try:
                if not _read_only([part], proof):
                    continue
            except UnicodeError:
                self.invalid = True
                return
            receipt = payload.get("terminal_receipt")
            path, sha = payload["path"], payload["source_sha256"]
            if (not isinstance(receipt, dict)
                    or receipt.get("termination_origin") != "in_process_source_read"
                    or self.final.get(path) != sha
                    or path not in self.mutation_indices):
                continue
            returned = _read_lines(payload)
            if not returned:
                continue
            if self.totals.setdefault(path, payload["total_lines"]) != payload["total_lines"]:
                self.invalid = True
                return
            # Count characters before copying/encoding an arbitrarily large read body.
            if sum(len(text) + 1 for _, text in returned) > MAX_REVIEW_CONTEXT_BYTES:
                self.invalid = True
                return
            groups: list[list[tuple[int, str]]] = []
            for number, text in returned:
                physical = text + ("\n" if number < payload["end_line"] or payload["ends_with_newline"] else "")
                key = (path, number)
                if key in self.verifier_seen and self.verifier_seen[key] != physical:
                    self.invalid = True
                    return
                if key in self.seen:
                    if self.seen[key] != physical:
                        self.invalid = True
                        return
                    continue  # Keep the first source event for an identical physical line.
                self.seen[key] = physical
                if not groups or number != groups[-1][-1][0] + 1:
                    groups.append([])
                groups[-1].append((number, physical))
            for group in groups:
                start, end = group[0][0], group[-1][0]
                key = (path, sha, start, end)
                row = {"path": path, "source_sha256": sha, "start_line": start, "end_line": end,
                       "text": "".join(text for _, text in group),
                       "source_event": {"event_index": index, "event_sha256": event_sha256,
                                        "part_index": part_index}}
                try:
                    self.bytes += len(caller._canonical_bytes(row))
                except UnicodeError:
                    self.invalid = True
                    return
                if (self.bytes > MAX_REVIEW_CONTEXT_BYTES
                        or self.bytes + self.verifier_bytes > MAX_PAGE_BYTES):
                    self.invalid = True
                    return
                self.ranges[key] = row

    def project(self, terminal: dict[str, Any], verifier_calls: dict[str, int]) -> dict[str, Any] | None:
        if (self.invalid or not (self.ranges or self.verifiers)
                or any(verifier_calls.get(call_id) != 1 for call_id in self.verifier_calls)):
            return None
        context = {"schema_version": "nokiy_parent_review_context_v1",
                   "scope": "recorded_partial_postimage", "coverage": "returned_physical_lines_only",
                   "omission": "all_unlisted_lines_files_and_dependencies",
                   "complete_file_coverage": False, "dependency_coverage": False,
                   "live_source_freshness": False, "read_permission": False, "parent_acceptance": False,
                   "terminal_content_sha256": caller._canonical_sha256(terminal),
                   "ranges": list(self.ranges.values())}
        if self.verifiers:
            context["coverage"] = "per_source_postimages_schema_and_returned_physical_lines_only"
            context["verifier_contexts"] = self.verifiers
        limit = MAX_PAGE_BYTES if self.verifiers else MAX_REVIEW_CONTEXT_BYTES
        return context if len(caller._canonical_bytes(context)) <= limit else None


def _verifier_excerpt(parts: list[tuple[str, str, dict[str, Any]]]) -> str | None:
    for kind, _, payload in reversed(parts):
        if kind != "focused_verifier" or any(key in payload for key in (
                "source_sha256", "path", "results", "changes", "file_changes", "postimage", "postimages")):
            continue
        text = "\n".join(value for key in ("stdout", "stderr")
                         if isinstance(value := payload.get(key), str) and value)
        if text:
            return text.encode("utf-8")[-MAX_DIAGNOSTIC_BYTES:].decode("utf-8", errors="ignore")
    return None


def _display_commands(result, commands, selected, offset, *, command_stream="history",
                      source_reads=frozenset()) -> None:
    displayed = commands
    if selected:
        matches = {sha: sum(row["command_sha256"] == sha for row in commands) for sha in selected}
        result["command_selection"] = {
            "scope": "display_only_all_evidence_checked", "matches": matches,
            "missing": [sha for sha, count in matches.items() if not count],
        }
        displayed = [row for row in commands if row["command_sha256"] in matches]
        if result["command_selection"]["missing"] and result["first_blocker"] is None:
            result["first_blocker"] = "REQUESTED_COMMAND_NOT_FOUND"
            result["status"] = "INCOMPLETE_EVIDENCE"
    if command_stream == "priority":
        retained = [row for row in displayed if row["event_index"] not in source_reads]
        result["pagination"].update(stream="priority",
                                    omitted_source_read_commands=len(displayed) - len(retained),
                                    total_commands=len(retained))
        # Failed or unproven commands lead the priority stream, then verifier results, so the first
        # page names what the parent must repair or accept without opening the artifacts; the
        # stable sort keeps event order within each rank.
        def rank(row):
            if row.get("execution_proof") != "known_success":
                return 0
            return 1 if "verifier_output_excerpt" in row else 2
        displayed = sorted(retained, key=rank)
    if offset > len(displayed):
        raise InspectionError("OFFSET_OUT_OF_RANGE")
    for command in displayed[offset:offset + PAGE_SIZE]:
        result["commands"].append(command)
        next_offset = offset + len(result["commands"])
        result["pagination"]["next_offset"] = next_offset if next_offset < len(displayed) else None
        if len(caller._canonical_bytes(result)) > MAX_PAGE_BYTES:
            result["commands"].pop()
            result["pagination"]["next_offset"] = next_offset - 1
            break
    if displayed and offset < len(displayed) and not result["commands"]:
        raise InspectionError("COMMAND_PAGE_TOO_LARGE")


def _gap_envelope(text: object) -> dict[str, Any] | None:
    """Decode only explicit, complete JSON envelopes; never infer a handoff from prose."""
    if not isinstance(text, str):
        return None
    custom_span = None
    marker = "```" + full_core.CAPABILITY_GAP_SCHEMA
    if marker in text:
        if text.count(marker) != 1:
            raise InspectionError("CAPABILITY_GAP_DUPLICATE_FENCE")
        match = re.search(r"(?m)^" + re.escape(marker) + r"\n(.*?)\n```(?:\n|\Z)", text, re.DOTALL)
        if match is None:
            raise InspectionError("CAPABILITY_GAP_FENCE_INVALID")
        custom_span = match.span(1)
    object_prefix = re.compile(r"[ \t\r\n]*\{")
    json_fence_count = 0
    def payloads():
        nonlocal json_fence_count
        if custom_span is not None:
            yield custom_span[0], custom_span[1], True
        for match in re.finditer(r"(?m)^```json\n(.*?)\n```(?:\n|\Z)", text, re.DOTALL):
            json_fence_count += 1
            yield match.start(1), match.end(1), False
        if object_prefix.match(text):
            # Decode the entire answer, including whitespace, not a prose fragment.
            yield 0, len(text), False
    handoff = None
    undecodable = None
    for start, end, custom in payloads():
        if not custom and object_prefix.match(text, start, end) is None:
            continue
        try:
            # Reject oversized spans before copying/encoding complete message text.
            if end - start > MAX_CAPABILITY_GAP_BYTES:
                raise InspectionError("CAPABILITY_GAP_TOO_LARGE")
            raw = text[start:end].encode("utf-8")
            if len(raw) > MAX_CAPABILITY_GAP_BYTES:
                raise InspectionError("CAPABILITY_GAP_TOO_LARGE")
            data = _json(raw)
        except InspectionError as exc:
            if custom or text.find(full_core.CAPABILITY_GAP_SCHEMA, start, end) != -1:
                raise
            if undecodable is None:
                undecodable = exc.code
            continue
        if custom or data.get("schema_version") == full_core.CAPABILITY_GAP_SCHEMA:
            if handoff is not None:
                raise InspectionError("CAPABILITY_GAP_DUPLICATE_FENCE")
            handoff = data
    incomplete_fence = json_fence_count != sum(1 for _ in re.finditer(r"(?m)^```json(?:\n|\Z)", text))
    if incomplete_fence and (handoff is not None or full_core.CAPABILITY_GAP_SCHEMA in text):
        raise InspectionError("CAPABILITY_GAP_FENCE_INVALID")
    if handoff is not None and undecodable is not None:
        raise InspectionError(undecodable)
    return handoff


def _gap_decode(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Retain a bounded candidate, absence, or error code, never text or a traceback."""
    try:
        return _gap_envelope(text), None
    except InspectionError as exc:
        return None, exc.code
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return None, "CAPABILITY_GAP_SCHEMA_INVALID"


def _has_capability_gap(terminal: dict[str, Any]) -> bool:
    text = terminal.get("result_text")
    if (terminal.get("result_truncated") is True or
            isinstance(text, str) and "```" + full_core.CAPABILITY_GAP_SCHEMA in text):
        return True
    try:
        return _gap_envelope(text) is not None
    except (InspectionError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return True


def _gap_diagnostic(blocker: str | None = None) -> dict[str, Any]:
    return {"schema_version": full_core.CAPABILITY_GAP_SCHEMA,
            "status": "UNAVAILABLE" if blocker else "MODEL_REPORTED",
            "first_blocker": blocker, "trust": "model_reported_diagnostic",
            "requires_parent_readmission": True, "permission_grant": False,
            "authority_effect": "none", "execution_proof": "unproven",
            "retry_safe": False, "mission_acceptance": "parent_owned"}


def _gap_path(value: object, workspace: object, *, file_target: bool = False) -> str:
    if (full_core._exact_presentation_path(value, absolute=False) is None
            or len(value.encode("utf-8")) > 256 or value != value.strip()
            or ":" in value or any(part.casefold() == ".git" for part in value.split("/"))):
        raise InspectionError("CAPABILITY_GAP_PATH_INVALID")
    if full_core._exact_presentation_path(workspace, absolute=True) is None:
        raise InspectionError("CAPABILITY_GAP_PATH_UNVERIFIED")
    root = Path(workspace)
    try:
        if not stat.S_ISDIR(root.lstat().st_mode) or root.resolve(strict=True) != root:
            raise InspectionError("CAPABILITY_GAP_PATH_UNSAFE")
        current = root
        parts = value.split("/")
        for index, part in enumerate(parts):
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                # An exact absent target is only a proposal; new admission must recheck it.
                break
            final = index == len(parts) - 1
            if (stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
                    or not final and not stat.S_ISDIR(mode)
                    or final and file_target and not stat.S_ISREG(mode)):
                raise InspectionError("CAPABILITY_GAP_PATH_UNSAFE")
    except OSError as exc:
        raise InspectionError("CAPABILITY_GAP_PATH_UNVERIFIED") from exc
    return value


def _gap_diff(value: object, workspace: object, targets: dict[str, set[str]]) -> None:
    """Validate compact plain unified diffs, never apply them or prove their preimages."""
    if (not isinstance(value, str) or not value.endswith("\n")
            or len(value.encode("utf-8")) > MAX_CAPABILITY_DIFF_BYTES or "\x00" in value):
        raise InspectionError("CAPABILITY_GAP_DIFF_INVALID")
    lines, index, changed, seen = value.split("\n")[:-1], 0, False, set()
    while index < len(lines):
        if (not lines[index].startswith("--- ") or index + 1 >= len(lines)
                or not lines[index + 1].startswith("+++ ")):
            raise InspectionError("CAPABILITY_GAP_DIFF_INVALID")
        paths = []
        for header, prefix in ((lines[index][4:], "a/"), (lines[index + 1][4:], "b/")):
            if header == "/dev/null":
                paths.append(None)
            elif header.startswith(prefix):
                paths.append(_gap_path(header[len(prefix):], workspace, file_target=True))
            else:
                raise InspectionError("CAPABILITY_GAP_DIFF_INVALID")
        old, new = paths
        target = new or old
        operation = "create" if old is None else "delete" if new is None else "modify"
        if (target is None or old is not None and new is not None and old != new
                or operation not in targets.get(target, set()) or target in seen):
            raise InspectionError("CAPABILITY_GAP_DIFF_INVALID")
        seen.add(target)
        index += 2
        hunks, old_end, new_end = 0, 0, 0
        while index < len(lines) and lines[index].startswith("@@ "):
            match = re.fullmatch(r"@@ -([0-9]{1,9})(?:,([0-9]{1,9}))? "
                                 r"\+([0-9]{1,9})(?:,([0-9]{1,9}))? @@(?: .*)?", lines[index])
            if match is None:
                raise InspectionError("CAPABILITY_GAP_DIFF_INVALID")
            old_start, old_count, new_start, new_count = (
                int(part) if part is not None else 1 for part in match.groups())
            if (old_start < old_end or new_start < new_end
                    or old_count and old_start == 0 or new_count and new_start == 0
                    or old is None and old_count or new is None and new_count):
                raise InspectionError("CAPABILITY_GAP_DIFF_INVALID")
            old_end, new_end = old_start + old_count, new_start + new_count
            index += 1
            previous_body = False
            while index < len(lines):
                line = lines[index]
                if line == "\\ No newline at end of file" and previous_body:
                    previous_body = False
                    index += 1
                    continue
                if old_count == new_count == 0:
                    break
                if not line or line[0] not in " +-":
                    raise InspectionError("CAPABILITY_GAP_DIFF_INVALID")
                old_count -= line[0] in " -"
                new_count -= line[0] in " +"
                changed |= line[0] in "+-"
                if old_count < 0 or new_count < 0:
                    raise InspectionError("CAPABILITY_GAP_DIFF_INVALID")
                previous_body = True
                index += 1
            if old_count or new_count:
                raise InspectionError("CAPABILITY_GAP_DIFF_INVALID")
            hunks += 1
        if not hunks:
            raise InspectionError("CAPABILITY_GAP_DIFF_INVALID")
    if not changed:
        raise InspectionError("CAPABILITY_GAP_DIFF_INVALID")


def _capability_gap(terminal: dict[str, Any], original: dict[str, Any] | None,
                    terminal_sha256: str, *,
                    verified_message_gap: tuple[dict[str, Any] | None, str | None] | None = None) -> dict[str, Any] | None:
    """Validate an inspected complete-message outcome or the untruncated preview."""
    if terminal.get("result_truncated") is True and verified_message_gap is None:
        gap = _gap_diagnostic("CAPABILITY_GAP_RESULT_TRUNCATED")
        if terminal.get("result_artifact") is not None:
            gap["result_artifact"] = terminal["result_artifact"]
    elif original is None or (terminal.get("result_truncated") is not False and verified_message_gap is None):
        if not _has_capability_gap(terminal):
            return None
        gap = _gap_diagnostic("CAPABILITY_GAP_REQUEST_BINDING_UNAVAILABLE" if original is None
                              else "CAPABILITY_GAP_TRUNCATION_UNPROVEN")
    else:
        try:
            data, error = (verified_message_gap if verified_message_gap is not None else
                           (_gap_envelope(terminal.get("result_text")), None))
            if error is not None:
                raise InspectionError(error)
            if data is None:
                return None
            if (set(data) != {"schema_version", "missing_capabilities", "completed_work",
                             "remaining_work", "unified_diff"}
                    or data["schema_version"] != full_core.CAPABILITY_GAP_SCHEMA):
                raise InspectionError("CAPABILITY_GAP_SCHEMA_INVALID")
            for key in ("completed_work", "remaining_work"):
                items = data[key]
                if (not isinstance(items, list) or not (0 if key == "completed_work" else 1) <= len(items) <= 8
                        or any(not isinstance(item, str) or not item.strip() or "\x00" in item
                               or len(item.encode("utf-8")) > 512 for item in items)):
                    raise InspectionError("CAPABILITY_GAP_SCHEMA_INVALID")
            missing, seen = data["missing_capabilities"], set()
            targets: dict[str, set[str]] = {}
            if not isinstance(missing, list) or not 1 <= len(missing) <= 8:
                raise InspectionError("CAPABILITY_GAP_SCHEMA_INVALID")
            for item in missing:
                if (not isinstance(item, dict) or set(item) != {"path", "operation", "tool"}
                        or not isinstance(item["operation"], str)
                        or item["operation"] not in {"read", "modify", "create", "delete", "command"}
                        or not isinstance(item["tool"], str)
                        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", item["tool"]) is None):
                    raise InspectionError("CAPABILITY_GAP_SCHEMA_INVALID")
                path = item["path"]
                if path is None:
                    if item["operation"] != "command":
                        raise InspectionError("CAPABILITY_GAP_PATH_INVALID")
                else:
                    _gap_path(path, original.get("workspace"))
                entry = (path, item["operation"], item["tool"])
                if entry in seen:
                    raise InspectionError("CAPABILITY_GAP_SCHEMA_INVALID")
                seen.add(entry)
                if path is not None and item["operation"] in {"modify", "create", "delete"}:
                    targets.setdefault(path, set()).add(item["operation"])
            if (data["unified_diff"] is None and targets
                    and not any(item["operation"] == "read" for item in missing)):
                raise InspectionError("CAPABILITY_GAP_PATCH_OR_READ_EVIDENCE_REQUIRED")
            if data["unified_diff"] is not None:
                _gap_diff(data["unified_diff"], original.get("workspace"), targets)
            gap = _gap_diagnostic()
            gap["handoff"] = data
        except InspectionError as exc:
            gap = _gap_diagnostic("CAPABILITY_GAP_INVALID_JSON" if exc.code == "INVALID_JSON" else exc.code)
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            gap = _gap_diagnostic("CAPABILITY_GAP_SCHEMA_INVALID")
    if original is not None:
        gap["binding"] = {key: terminal[key] for key in ("request_id", "request_sha256", "native_thread_id")}
        gap["binding"].update(terminal_sha256=terminal_sha256,
                              terminal_content_sha256=caller._canonical_sha256(terminal))
    return gap


def inspect(artifact_root: Path, request_id: str, expected_thread_id: str,
            *, offset: int = 0, command_sha256: tuple[str, ...] = (),
            command_stream: str = "history") -> dict[str, Any]:
    """Return one fixed-size page. Re-reading does not extend execution expiry."""
    result: dict[str, Any] = {
        "schema_version": "nokiy_result_inspection_v1", "request_id": request_id,
        "expected_thread_id": expected_thread_id, "terminal_sha256": None,
        "status": "INCOMPLETE_EVIDENCE", "first_blocker": None,
        "mission_acceptance": "parent_owned", "artifact_integrity": "unproven",
        "command_success": "unproven", "artifacts": {}, "counts": {},
        "file_change_success": "unproven", "file_changes": None,
        "commands": [], "usage": None, "cleanup": {"historical_pass": None,
                                                    "engine_pid": "unknown", "supervisor_pid": "unknown"},
        "pagination": {"offset": offset, "limit": PAGE_SIZE, "next_offset": None},
    }
    if command_stream == "priority":
        result["pagination"]["stream"] = "priority"
    try:
        selected = _command_selection(command_sha256)
        if command_stream not in ("history", "priority"):
            raise InspectionError("COMMAND_STREAM_INVALID")
        if (not caller.REQUEST_ID_PATTERN.fullmatch(request_id)
                or not caller.THREAD_ID_PATTERN.fullmatch(expected_thread_id)
                or type(offset) is not int or offset < 0):
            raise InspectionError("INPUT_INVALID")
        root = Path(artifact_root).expanduser()
        if not root.is_absolute() or ".." in root.parts:
            raise InspectionError("ROOT_INVALID")
        _directory(root)
        # A root may itself be a caller-selected path; never follow a request symlink.
        root = root.resolve(strict=True)
        run = root / request_id
        _directory(run)
        terminal_raw = _file(run / "terminal.json", caller.MAX_TERMINAL_BYTES)
        decoded_terminal = _json(terminal_raw)
        terminal = caller.read_terminal(root, request_id)
        if decoded_terminal != terminal:
            raise InspectionError("TERMINAL_CHANGED")
        result["terminal_sha256"] = hashlib.sha256(terminal_raw).hexdigest()
        if terminal.get("native_thread_id") != expected_thread_id:
            raise InspectionError("THREAD_ID_MISMATCH")
        result["terminal_status"] = terminal.get("status") if terminal.get("status") in ("RESULT_AVAILABLE", "BLOCKED") else "UNKNOWN"
        detail = terminal.get("first_typed_blocker")
        result["terminal_first_blocker"] = detail[:256] if isinstance(detail, str) else None
        usage = terminal.get("usage")
        if isinstance(usage, dict):
            result["usage"] = {k: usage[k] for k in USAGE_FIELDS
                               if type(usage.get(k)) is int and 0 <= usage[k] < 10**18}
        result["cleanup"]["historical_pass"] = terminal.get("cleanup_pass") is True
        refs = (("supervision", "supervision.json", MAX_JSON),)
        data = {}
        for key, name, limit in refs:
            data[key], result["artifacts"][key] = _artifact(
                run, name, terminal.get(key + "_artifact"), limit)
        supervision = _json(data["supervision"])
        scope = supervision.get("scope")
        cleanup = terminal.get("cleanup")
        cleanup_bound = (isinstance(scope, dict) and isinstance(cleanup, dict)
                         and all(scope.get(k) == cleanup.get(k) for k in
                                 ("engine_pid", "supervisor_pid", "engine_reaped",
                                  "no_live_descendants", "cleanup_error", "isolation_source"))
                         and scope.get("engine_reaped") is True
                         and scope.get("no_live_descendants") is True
                         and scope.get("cleanup_error") is None)
        if isinstance(scope, dict):
            result["cleanup"]["engine_pid"] = _process(scope.get("engine_pid"))
            result["cleanup"]["supervisor_pid"] = _supervisor_process(scope, bound=cleanup_bound)
        original, expected_turn = None, None
        identity_path = run / "request-identity.json"
        # Removing a receipt label cannot downgrade a retained request binding.
        full_core_receipt = (terminal.get("execution_model") == "single_task_full_core"
                             or identity_path.exists() or identity_path.is_symlink())
        if full_core_receipt:
            original = _json(_file(identity_path, MAX_JSON))
            payload = {k: v for k, v in original.items() if k not in ("request_id", "request_sha256")}
            if (original.get("schema_version") != caller.REQUEST_SCHEMA_VERSION
                    or original.get("execution_profile") not in {"direct", "balanced"}
                    or caller._canonical_sha256(payload) != terminal.get("request_sha256")
                    or original.get("request_id") != terminal.get("request_id")
                    or original.get("request_sha256") != terminal.get("request_sha256")
                    or any(original.get(k) != terminal.get(k) for k in
                           ("native_thread_id", "model", "reasoning_effort", "execution_profile"))):
                raise InspectionError("TRAJECTORY_REQUEST_IDENTITY_MISMATCH")
            try:
                expected_turn = full_core._terminal_identity(original)
            except (KeyError, TypeError) as exc:
                raise InspectionError("TRAJECTORY_REQUEST_IDENTITY_MISMATCH") from exc
            if expected_turn["service_tier"] != terminal.get("requested_service_tier"):
                raise InspectionError("TRAJECTORY_REQUEST_IDENTITY_MISMATCH")
            trajectory_limit = original.get("max_trajectory_bytes")
            if trajectory_limit is not None and (
                    type(trajectory_limit) is not int or not 1024 <= trajectory_limit <= caller.MAX_TRAJECTORY_BYTES):
                raise InspectionError("TRAJECTORY_BUDGET_INVALID")
        result_limit = original.get("max_result_bytes") if original is not None else caller.MAX_RESULT_BYTES
        if type(result_limit) is not int or not 256 <= result_limit <= caller.MAX_RESULT_BYTES:
            raise InspectionError("RESULT_BUDGET_INVALID")
        if (terminal.get("status") == "BLOCKED"
                and detail in ("NOKIY_FULL_CORE_SERVICE_NOT_READY", "GRAPH_BATCH_PEER_FAILED")
                and terminal.get("trajectory_artifact") is None):
            # A service refusal or peer cancellation can precede CLI creation.
            # Verify retained evidence without inventing a trajectory or usage.
            engine = supervision.get("result")
            result["usage"] = None
            if (not isinstance(engine, dict)
                    or any(terminal.get(key) is not None for key in (
                        "usage", "observed_model", "observed_service_tier", "tool_loop",
                        "command_evidence_summary"))
                    or terminal.get("result_text") not in (None, "")
                    or any(engine.get(key) is not None for key in (
                        "usage", "trajectory_artifact", "command_evidence", "tool_loop",
                        "provider_observation", "terminal_evidence", "result_artifact"))
                    or engine.get("turn_completed") is True
                    or engine.get("final_text") not in (None, "")):
                raise InspectionError("PRE_CLI_EVIDENCE_CONFLICT")
            core_path = run / "core.jsonl"
            stderr_path = run / "core.stderr"
            if (core_path.exists() or core_path.is_symlink()
                    or stderr_path.exists() or stderr_path.is_symlink()
                    or any(terminal.get(key) is not None for key in (
                        "command_evidence_artifact", "file_change_evidence_artifact", "result_artifact"))):
                raise InspectionError("ARTIFACT_REFERENCE_MISSING")
            if supervision.get("failure") != detail:
                raise InspectionError("SUPERVISION_FAILURE_MISMATCH")
            diagnostic = terminal.get("startup_failure_diagnostic")
            if diagnostic is not None:
                engine = supervision.get("result")
                if (not isinstance(diagnostic, dict) or not isinstance(engine, dict)
                        or set(diagnostic) != {"schema_version", "service", "reason", "exit_code", "os_errno",
                                              "log_artifact", "log_tail", "log_truncated", "log_capture_blocker"}
                        or diagnostic != engine.get("startup_failure_diagnostic")
                        or diagnostic.get("schema_version") != "nokiy_full_core_startup_failure_v1"
                        or detail != "NOKIY_FULL_CORE_SERVICE_NOT_READY"
                        or not isinstance(diagnostic.get("service"), str)
                        or diagnostic["service"] not in {"tura_session_db", "tura_router"}
                        or not isinstance(diagnostic.get("reason"), str)
                        or diagnostic["reason"] not in {"spawn_failed", "process_exited", "readiness_timeout"}
                        or diagnostic["exit_code"] is not None and type(diagnostic["exit_code"]) is not int
                        or diagnostic["os_errno"] is not None and type(diagnostic["os_errno"]) is not int
                        or diagnostic["log_capture_blocker"] is not None and (
                            not isinstance(diagnostic["log_capture_blocker"], str)
                            or len(diagnostic["log_capture_blocker"]) > 256)):
                    raise InspectionError("STARTUP_DIAGNOSTIC_INVALID")
                reference = diagnostic.get("log_artifact")
                if reference is not None:
                    if type(diagnostic.get("log_truncated")) is not bool:
                        raise InspectionError("STARTUP_DIAGNOSTIC_INVALID")
                    full_core._artifact_reference(run / (diagnostic["service"] + ".log"), reference)
                    log = full_core._startup_log(run, diagnostic["service"], reference=reference)
                    if any(diagnostic.get(key) != value for key, value in log.items()):
                        raise InspectionError("STARTUP_DIAGNOSTIC_MISMATCH")
                    result["artifacts"]["startup_log"] = reference
                elif diagnostic.get("log_tail") is not None or diagnostic.get("log_truncated") is not None:
                    raise InspectionError("STARTUP_DIAGNOSTIC_INVALID")
                result["startup_failure_diagnostic"] = diagnostic
            if _file(run / "terminal.json", caller.MAX_TERMINAL_BYTES) != terminal_raw:
                raise InspectionError("TERMINAL_CHANGED")
            result.update(first_blocker=detail, trajectory_status="absent", artifact_integrity="verified")
            return result
        trajectory_ref = full_core._artifact_reference(run / "core.jsonl", terminal.get("trajectory_artifact"))
        events = full_core._Trajectory(run / "core.jsonl", reference=trajectory_ref)
        if original is not None and terminal.get("status") == "RESULT_AVAILABLE":
            events.limit = trajectory_limit
        capability_request = original  # Retain the already checked binding, not later proof-local reads.
        turn = full_core._TurnSummary(expected_turn, require_completed=terminal.get("status") == "RESULT_AVAILABLE")
        delivery = full_core._TerminalEvidence(original if full_core_receipt else None)
        handoff = full_core._EvidenceHandoff(original, run, readback=True)
        failure = (full_core._FailedTurnDiagnostic(original)
                   if original is not None and terminal.get("status") == "BLOCKED"
                   and "provider_failure_diagnostic" in terminal else None)
        event_count = file_count = 0
        command_ids = {}
        last_mutation_index = -1
        mutation_positions = []
        def record_mutation_position(item, output):
            if any(key in item for key in ("runtime_id", "provider_tool_call_id", "command_id",
                                           "command_run_id", "command_index", "step")):
                mutation_positions.append(_own_edit_position(item, output))

        message_preview, message_truncated, message_ref = None, False, None
        message_gap = None
        message_answer = None
        def observations():
            nonlocal event_count, file_count, last_mutation_index
            nonlocal message_preview, message_truncated, message_ref, message_gap
            nonlocal message_answer
            for event in failure.events(events) if failure is not None else events:
                event_count += 1
                delivery.observe(event)
                handoff.observe(event, event_count - 1)
                turn.observe(event)
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "command_execution":
                    command_id = item.get("command_id")
                    if isinstance(command_id, str):
                        command_ids[command_id] = command_ids.get(command_id, 0) + 1
                file_count += isinstance(item, dict) and item.get("type") == "file_change"
                if isinstance(item, dict) and item.get("type") == "file_change":
                    last_mutation_index = event_count - 1
                    record_mutation_position(item, full_core._structured_output(item))
                if (full_core_receipt and event.get("type") == "item.completed"
                        and isinstance(item, dict) and item.get("type") == "assistant_message"):
                    text = item.get("text", "")
                    if not isinstance(text, str):
                        raise InspectionError("NOKIY_FULL_CORE_RESULT_INVALID")
                    message_preview, message_truncated = full_core._text_preview(text, result_limit)
                    message_ref = (full_core._message_reference(run / "last-message.txt", text)
                                   if message_truncated else None)
                    message_answer = (text if message_truncated and not delivery.enabled
                                      and message_ref["bytes"] <= MAX_PAGE_BYTES else None)
                    # Overwrite even with absence/error: only the LAST completed
                    # message can supply a handoff, after all later integrity checks.
                    message_gap = (_gap_decode(text) if terminal.get("status") == "RESULT_AVAILABLE" else None)
                    del text
                yield event
        expected_evidence = full_core._command_evidence(observations())
        if failure is not None:
            diagnostic = full_core.provider_failure_diagnostic_with_snapshot(
                failure.finish(events.reference), terminal, run, require_existing=True)
            if diagnostic is not None:
                result["provider_failure_diagnostic_sha256"] = caller._canonical_sha256(diagnostic)
        result["artifacts"]["trajectory"] = {"path": "core.jsonl", "sha256": events.reference["sha256"],
                                              "bytes": events.reference["bytes"]}
        if full_core_receipt and terminal.get("status") == "RESULT_AVAILABLE":
            turn.finish()
            if turn.usage != terminal.get("usage"):
                raise InspectionError("TRAJECTORY_USAGE_MISMATCH")
            evidence_text = delivery.result_text()
            if evidence_text is not None:
                message_preview, message_truncated, message_ref = evidence_text, False, None
                message_gap = None
                message_answer = None
            delivery.validate_projection(terminal)
            if (message_preview is None or message_preview != terminal.get("result_text")
                    or message_truncated is not terminal.get("result_truncated")
                    or delivery.marker is None and message_ref != terminal.get("result_artifact")):
                raise InspectionError("NOKIY_FULL_CORE_RESULT_INVALID")
        if delivery.marker is not None:
            handoff_fields = handoff.project(terminal, delivery.marker, inline_limit=result_limit)
            if handoff_fields:
                result["evidence_handoff"] = {
                    "terminal_content_sha256": caller._canonical_sha256(terminal), **handoff_fields}
                result_ref = handoff_fields["result_artifact"]
                if result_ref is not None:
                    result["artifacts"]["result"] = {"path": "worker-handoff.txt", "sha256": result_ref["sha256"],
                                                      "bytes": result_ref["bytes"]}
        elif terminal.get("result_truncated") is True or terminal.get("result_artifact") is not None:
            _, _, result_ref = full_core._result_projection(
                run, terminal.get("result_text"), terminal.get("result_truncated"), terminal.get("result_artifact"), result_limit)
            result["artifacts"]["result"] = {"path": "last-message.txt", "sha256": result_ref["sha256"],
                                              "bytes": result_ref["bytes"]}
        if terminal.get("status") == "BLOCKED" and terminal.get("command_evidence_artifact") is None:
            # No replay and no terminal rewrite. Only settled, request-local DB
            # observations can supplement a failed execution's missing projection.
            if not cleanup_bound or not _cleanup_settled(result["cleanup"]):
                raise InspectionError("CLEANUP_UNPROVEN")
            from . import durable_recovery
            recovered = durable_recovery.recover(run, terminal, events)
            commands = recovered["commands"]
            result["artifacts"].update(recovered["artifacts"])
            result["durable_recovery"] = recovered["details"]
            result["source_read_efficiency"] = recovered["read_efficiency"]
            result["counts"] = {"commands": len(commands),
                                "file_changes": len(recovered["details"]["file_changes"]),
                                "index_complete": recovered["details"]["command_index_complete"]}
            result["command_success"] = ("known_success" if commands and all(
                c["execution_proof"] == "known_success" for c in commands) else
                "known_failure" if any(c["execution_proof"] == "known_failure" for c in commands) else "unproven")
            result["first_blocker"] = "TERMINAL_NOT_AVAILABLE"
            if recovered["first_blocker_detail"] is not None:
                result.update(first_blocker="RECEIPT_PROOF_INVALID",
                              first_blocker_detail=recovered["first_blocker_detail"], command_success="unproven",
                              task_success=False, automatic_retry_allowed=False, execution_replayed=False)
            result["observation_availability"] = recovered["details"]["observation_availability"]
            result["aggregate_execution_proof"] = "unproven"
            _display_commands(result, commands, selected, offset, command_stream=command_stream)
            return result
        data["command_evidence"], result["artifacts"]["command_evidence"] = _artifact(
            run, "command-evidence.json", terminal.get("command_evidence_artifact"), MAX_JSON)
        evidence = _json(data["command_evidence"])
        file_ref = terminal.get("file_change_evidence_artifact")
        file_summary = terminal.get("file_change_evidence_summary")
        file_blocker = None
        file_postimages = None
        file_mutation_indices = None
        bound_contract = None
        if file_count or file_ref is not None or file_summary is not None:
            if file_ref is None or file_summary is None:
                file_blocker = "FILE_CHANGE_PROOF_MISSING"
            else:
                try:
                    raw, result["artifacts"]["file_change_evidence"] = _artifact(
                        run, "file-change-evidence.json", file_ref, MAX_JSON)
                    proof = _json(raw)
                    original = _json(_file(run / "request-identity.json", MAX_JSON))
                    contract = _inspection_contract(run, terminal, original, result["artifacts"],
                                                    require_artifact=True)
                    verified = file_change_evidence.verify(proof, events, terminal, original, contract)
                    if (not isinstance(file_summary, dict) or set(file_summary) != {"total_count", "target_count"}
                            or type(file_summary["total_count"]) is not int
                            or type(file_summary["target_count"]) is not int
                            or file_summary != {"total_count": verified["events"],
                                                "target_count": verified["targets"]}):
                        raise InspectionError("FILE_CHANGE_COUNTS_MISMATCH")
                    result["file_changes"] = verified
                    bound_contract = contract
                    file_postimages = proof["postimages"]
                    # verify checked every normalized path and event identity in order.
                    # These recorded fences are not live source freshness.
                    file_mutation_indices = {change["path"]: row["event_index"]
                                             for row in proof["records"] for change in row["changes"]}
                    result["file_change_success"] = "known_success"
                except file_change_evidence.EffectError as exc:
                    file_blocker = str(exc)
                except InspectionError as exc:
                    file_blocker = exc.code
        total_commands = expected_evidence["total_count"]
        expected_indices = [r["event_index"] for r in expected_evidence["records"]]
        records = evidence.get("records")
        if (not isinstance(records, list) or type(evidence.get("total_count")) is not int
                or type(evidence.get("failed_count")) is not int
                or type(evidence.get("complete")) is not bool
                or evidence["total_count"] != total_commands
                or evidence["complete"] != (total_commands <= full_core.MAX_COMMAND_EVIDENCE)
                or len(records) != min(total_commands, full_core.MAX_COMMAND_EVIDENCE)
                or terminal.get("command_evidence_summary") != {key: evidence[key]
                    for key in ("total_count", "failed_count", "complete")}):
            raise InspectionError("EVIDENCE_COUNTS_MISMATCH")
        if any(not isinstance(r, dict) or type(r.get("event_index")) is not int or r.get("event_index") != i
               for r, i in zip(records, expected_indices)):
            raise InspectionError("EVIDENCE_INDEX_MISMATCH")
        failures = 0
        commands = []
        source_reads = set()
        reads = _ReadEfficiency()
        review = (_ParentReviewContext(bound_contract, file_postimages, capability_request,
                                       file_mutation_indices)
                  if capability_request is not None
                  and bound_contract is not None and file_postimages is not None
                  and file_mutation_indices is not None else None)
        failed_indices, raw_failed_indices = set(), set()
        search_no_match, digest_mismatch, inspect_failures, no_content_reads = set(), set(), set(), set()
        verifier_failures, verifier_passes, verifier_calls = {}, [], {}
        verifier_identity = {"request_id": terminal["request_id"], "request_sha256": terminal["request_sha256"],
                             "session_id": "full-" + terminal["request_sha256"],
                             "native_thread_id": expected_thread_id}
        rejection_candidates, rejection_calls = {}, {}
        refusal_candidates, refusal_calls = {}, {}
        postgate_denials, postgate_receipts = {}, set()
        postgate_unindexed = False
        checked_parts = {}
        by_index = {record["event_index"]: record for record in records}
        for index, event in enumerate(events):
            record = by_index.get(index)
            if record is None:
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "command_execution":
                    # The index parser excludes native task bookkeeping, including
                    # JSON-string outputs without command_type. Only settled,
                    # matching status-only controls can bypass the unknown-effect fence.
                    if not _native_task_status(event, command_ids):
                        # No checked command identity/settlement: do not guess a link
                        # to another event or retain raw context across unknown effects.
                        postgate_unindexed = True
                        if review is not None:
                            review.reset_reads()
                            review.raw_blocked = True
                continue
            item = event["item"]
            command = item.get("command")
            if (record.get("event_sha256") != caller._canonical_sha256(event)
                    or not isinstance(command, str)
                    or record.get("command_sha256") != hashlib.sha256(command.encode()).hexdigest()
                    or record.get("success") is not full_core._successful_observation(item)):
                raise InspectionError("EVIDENCE_IDENTITY_MISMATCH")
            if (record.get("exit_code") is not None and type(record["exit_code"]) is not int):
                raise InspectionError("EVIDENCE_EXIT_MISMATCH")
            failures += record["success"] is False
            if record["success"] is False:
                raw_failed_indices.add(index)
            output = full_core._structured_output(item)
            for call_id in _command_call_ids(item, output):
                rejection_calls[call_id] = rejection_calls.get(call_id, 0) + 1
            codes = [value.get("exit_code") for value in (item, output)
                     if "exit_code" in value and value["exit_code"] is not None]
            if any(type(code) is not int for code in codes):
                raise InspectionError("EXIT_CODE_INVALID")
            receipt_state = _receipt(run, output, item=item, event_index=index)
            child_receipts: list[str] = []
            child_proofs = _checked_results(run, output, child_receipts, item=item, event_index=index)
            if receipt_state == "matched":
                codes.append(output["terminal_receipt"]["exit_code"])
            if len(set(codes)) > 1:
                raise InspectionError("EXIT_CODE_CONFLICT")
            exit_code = codes[0] if codes else None
            if record.get("exit_code") != (codes[0] if codes and len(set(codes)) == 1 else None):
                raise InspectionError("EVIDENCE_EXIT_MISMATCH")
            diagnostic = output.get("error") or output.get("stderr")
            if not output and record["success"] is False:
                diagnostic = item.get("aggregated_output")
            status = item.get("status")
            proof = ("known_success" if exit_code == 0 and receipt_state == "matched"
                     and record["success"] is True else
                     "known_failure" if exit_code is not None and exit_code != 0 else "unavailable")
            if output.get("status") == "failed" or "known_failure" in child_proofs:
                proof = "known_failure"
            elif proof == "known_success" and (not _settled_output(output)
                                               or any(p != "known_success" for p in child_proofs)):
                proof = "unavailable"
            commands.append({"event_index": index, "event_sha256": record["event_sha256"],
                             "command_sha256": record["command_sha256"],
                             "command": command if len(command.encode()) <= MAX_COMMAND_BYTES else None,
                             **({"command_head": _command_head(command)}
                                if len(command.encode()) > MAX_COMMAND_BYTES else {}),
                             "command_bytes": len(command.encode()),
                             "status": status[:32] if isinstance(status, str) else None,
                             "diagnostic_excerpt": diagnostic[:512] if isinstance(diagnostic, str) else None,
                             "exit_code": exit_code, "receipt_check": receipt_state,
                             "execution_proof": proof})
            if record["success"] is False or proof == "known_failure":
                failed_indices.add(index)
            if (proof == "known_failure" and receipt_state == "matched" and exit_code == 1
                    and "results" not in output and _search_no_match(command, output)):
                search_no_match.add(index)
            if (proof == "known_failure" and receipt_state == "matched" and exit_code == 1
                    and "results" not in output and capability_request is not None
                    and _source_read_digest_mismatch(item, command, output)):
                try:
                    if bound_contract is None:
                        bound_contract = _inspection_contract(run, terminal, capability_request,
                            result["artifacts"], require_artifact=False)
                    if _outside_write_scope(output["source_read_evidence"]["request"].get("path"), bound_contract):
                        digest_mismatch.add(index)
                except (InspectionError, OSError, ValueError, TypeError, KeyError):
                    pass  # Without the bound contract the failure stays unexplained.
            if (proof == "known_failure" and receipt_state == "matched" and "results" not in output
                    and capability_request is not None
                    and (_inspect_shell_failure(item, output) or _parent_shell_failure(item, output))):
                try:
                    if bound_contract is None:
                        bound_contract = _inspection_contract(run, terminal, capability_request,
                            result["artifacts"], require_artifact=False)
                    if ((_inspect_shell_failure(item, output) and _admitted_inspect_contract(bound_contract))
                            or (_parent_shell_failure(item, output)
                                and _admitted_parent_shell_contract(bound_contract))):
                        inspect_failures.add(index)
                except (InspectionError, OSError, ValueError, TypeError, KeyError):
                    pass  # Without the bound contract the failure stays unexplained.
            if (proof == "known_failure" and receipt_state == "matched" and exit_code == 1
                    and "results" not in output and capability_request is not None
                    and (read_path := _source_read_without_content(item, command, output)) is not None):
                try:
                    if bound_contract is None:
                        bound_contract = _inspection_contract(run, terminal, capability_request,
                            result["artifacts"], require_artifact=False)
                    if _outside_write_scope(read_path, bound_contract):
                        no_content_reads.add(index)
                except (InspectionError, OSError, ValueError, TypeError, KeyError):
                    pass  # Without the bound contract the failure stays unexplained.
            if "pre_execution_rejection" in output and capability_request is not None:
                try:
                    if bound_contract is None:
                        bound_contract = _inspection_contract(run, terminal, capability_request,
                            result["artifacts"], require_artifact=False)
                    rejection_candidates[index] = _source_read_rejection(
                        run, item, output, capability_request, bound_contract)
                except (InspectionError, OSError, ValueError, TypeError, KeyError, IndexError,
                        RecursionError, OverflowError):
                    pass  # An unauthenticated claim leaves the original failure blocked.
            if item.get("command_type") == "jspace" and capability_request is not None:
                call = item.get("command_id")
                if isinstance(call, str):
                    refusal_calls[call] = refusal_calls.get(call, 0) + 1
                try:
                    if bound_contract is None:
                        bound_contract = _inspection_contract(run, terminal, capability_request,
                            result["artifacts"], require_artifact=False)
                    refusal_candidates[index] = _jspace_refusal(run, item, capability_request, bound_contract)
                except (InspectionError, OSError, ValueError, TypeError, KeyError, IndexError,
                        RecursionError, OverflowError):
                    pass  # Without the router's witness the refusal stays an unexplained failure.
            if "policy_denial" in item and capability_request is not None:
                try:
                    if bound_contract is None:
                        bound_contract = _inspection_contract(run, terminal, capability_request,
                            result["artifacts"], require_artifact=False)
                    if _postgate_policy_denial(item, capability_request, bound_contract):
                        postgate_denials[index] = item["command_id"]
                except (InspectionError, OSError, ValueError, TypeError, KeyError):
                    pass  # Historical text and malformed metadata never establish no-start.
            if (receipt_state == "matched" and proof in ("known_success", "known_failure")
                    and output.get("outcome") in (None, "known")
                    and all(state == "matched" for state in child_receipts)
                    and all(child in ("known_success", "known_failure") for child in child_proofs)):
                postgate_receipts.add(index)
            parts = _typed_parts(item, output, receipt_state)
            if any(kind == "apply_patch" for kind, _, _ in parts):
                last_mutation_index = max(last_mutation_index, index)
                record_mutation_position(item, output)
                if review is not None:
                    review.reset()
            part_proofs = child_proofs if "results" in output else [proof]
            part_receipts = child_receipts if "results" in output else [receipt_state]
            checked_parts[index] = (parts, part_proofs, part_receipts)
            eligible, failed_verifiers = bool(parts) and len(parts) == len(part_proofs), []
            raw_context_safe = eligible
            for part_index, ((kind, line, payload), part_proof, part_receipt) in enumerate(
                    zip(parts, part_proofs, part_receipts)):
                observation = (_parent_verifier_observation(line, payload, part_receipt)
                               if kind == "focused_verifier" else None)
                if observation is not None:
                    if any(key in value and value[key] != expected
                           for value in (item, payload) for key, expected in verifier_identity.items()):
                        observation = None
                    elif "results" not in output and any(key in item for key in (
                            "runtime_id", "provider_tool_call_id", "command_id", "command_run_id",
                            "command_index", "step")):
                        position = _own_edit_position(item, payload)
                        if position is None:
                            observation = None
                        else:
                            observation["position"] = position
                if review is not None and (
                        part_receipt != "matched" or part_proof not in ("known_success", "known_failure")
                        or not (observation is not None or part_proof == "known_success" and (
                            kind == "task_status" or kind == "source_read"
                            and payload["terminal_receipt"].get("termination_origin") == "in_process_source_read"))):
                    raw_context_safe = False
                if observation is not None:
                    call_id = observation["call_id"]
                    verifier_calls[call_id] = verifier_calls.get(call_id, 0) + 1
                    if observation["exit_code"] == 0 and part_proof == "known_success":
                        verifier_passes.append((index, observation))
                        if review is not None:
                            review.add_verifier(payload, observation, index, record["event_sha256"],
                                                record["command_sha256"], part_index, last_mutation_index)
                if part_proof != "known_success":
                    if (part_proof == "known_failure" and observation is not None
                            and observation["exit_code"] > 0):
                        failed_verifiers.append(observation)
                    else:
                        eligible = False
            if (proof == "known_failure" and receipt_state == "matched" and eligible and failed_verifiers
                    and output.get("outcome") in (None, "known")
                    and output.get("isError", False) is False and output.get("error") is None):
                verifier_failures[index] = failed_verifiers
            if command_stream == "priority":
                if _read_only(parts, proof):
                    source_reads.add(index)
                excerpt = _verifier_excerpt(parts)
                if excerpt:
                    commands[-1]["verifier_output_excerpt"] = excerpt
            reads.add(item, output, receipt_state)
            if review is not None:
                if raw_context_safe:
                    review.add(parts, proof, index, record["event_sha256"])
                else:
                    # A settled opaque/mutating command is still a global raw-read
                    # barrier, including mixed batches with ambiguous effect order.
                    review.reset_reads()
        if evidence["complete"] and failures != evidence["failed_count"]:
            raise InspectionError("EVIDENCE_FAILURE_COUNT_MISMATCH")
        if not evidence["complete"] and not (failures <= evidence["failed_count"] <= total_commands):
            raise InspectionError("EVIDENCE_FAILURE_COUNT_MISMATCH")
        if evidence["failed_count"] != expected_evidence["failed_count"]:
            raise InspectionError("EVIDENCE_FAILURE_COUNT_MISMATCH")
        resolved, verifier_recoveries = {}, {}
        if (evidence["complete"] and file_blocker is None and capability_request is not None
                and verifier_failures and verifier_passes):
            try:
                if bound_contract is None:
                    bound_contract = _inspection_contract(run, terminal, capability_request,
                        result["artifacts"], require_artifact=False)
                sources = _resolution_sources(capability_request, bound_contract, file_postimages)
                if isinstance(bound_contract.get("verifier_commands"), list):
                    passes = [(i, row) for i, row in verifier_passes
                               if i > last_mutation_index and verifier_calls[row["call_id"]] == 1
                               and rejection_calls.get(row["call_id"]) == 1
                               and row["verifier_index"] < len(bound_contract["verifier_commands"])
                               # JSONL completion order cannot order parallel steps.
                               # Every native mutation must precede a same-batch PASS
                               # in a strictly earlier step; malformed positions fence it.
                               and all(position is not None and "position" in row
                                       and (position[0] != row["position"][0]
                                            or position[1] < row["position"][1])
                                       for position in mutation_positions)]
                    for failed_index, rows in verifier_failures.items():
                        links, unknown = [], []
                        for failed in rows:
                            candidates = [(i, row) for i, row in passes
                                          if i > failed_index and row["verifier_index"] == failed["verifier_index"]
                                          and verifier_calls[failed["call_id"]] == 1
                                          and rejection_calls.get(failed["call_id"]) == 1
                                          and ("position" not in row or "position" not in failed
                                               or row["position"][0] != failed["position"][0]
                                               or row["position"][1] > failed["position"][1])]
                            match = next(((i, row) for i, row in candidates if sources is not None
                                          and _verification_matches(row, bound_contract, sources)), None)
                            if match is None:
                                if candidates:
                                    passing_index, passed = candidates[-1]
                                    unknown.append({"passing_event_index": passing_index,
                                        "verifier_index": passed["verifier_index"],
                                        "failed_call_id": failed["call_id"], "passing_call_id": passed["call_id"],
                                        "reason": ("VERIFICATION_EVIDENCE_MISSING"
                                                   if passed["verification_evidence"] is None else
                                                   "SOURCE_COVERAGE_UNPROVEN" if sources is None else
                                                   "VERIFICATION_EVIDENCE_MISMATCH")})
                                continue
                            passing_index, passed = match
                            links.append({"passing_event_index": passing_index,
                                          "verifier_index": passed["verifier_index"],
                                          "verifier_sha256": passed["verification_evidence"]["verifier_sha256"],
                                          "failed_call_id": failed["call_id"], "passing_call_id": passed["call_id"]})
                        if len(links) == len(rows):
                            resolved[failed_index] = {"status": "resolved", "verifiers": links}
                        elif unknown:
                            # A later settled exit 0 is observable even when the producer
                            # omitted source/contract proof. It never discharges the failure.
                            verifier_recoveries[failed_index] = {"status": "UNKNOWN", "verifiers": unknown}
            except (InspectionError, OSError, ValueError, TypeError, KeyError, IndexError,
                    RecursionError, OverflowError):
                pass  # Missing optional proof never excuses the original failure.
        source_non_attempts = {index: proof for index, proof in rejection_candidates.items()
                               if rejection_calls[proof["call_id"]] == 1}
        # A reused call identity cannot be bound to one refusal.
        refusals = {index: proof for index, proof in refusal_candidates.items()
                    if refusal_calls.get(proof["call_id"]) == 1}
        non_attempts = {**source_non_attempts, **refusals}
        verifier_resolved_count = len(resolved)
        read_resolved, read_failure_count = {}, 0
        has_failed_source_read = any(
            kind == "source_read" and proof == "known_failure"
            for parts, proofs, _ in checked_parts.values()
            for (kind, _, _), proof in zip(parts, proofs)
        )
        if (has_failed_source_read and evidence["complete"]
                and file_blocker is None and capability_request is not None):
            try:
                if bound_contract is None:
                    bound_contract = _inspection_contract(run, terminal, capability_request,
                        result["artifacts"], require_artifact=False)
                read_resolved, read_failure_count = _resolve_own_edit_reads(
                    run, events, checked_parts, capability_request, bound_contract)
                resolved.update(read_resolved)
            except (InspectionError, OSError, ValueError, TypeError, KeyError, IndexError,
                    RecursionError, OverflowError):
                pass  # Optional content evidence never turns a failure into success.
        # A no-match search answered its question; that explains its own failed record.
        no_match = {index: {"status": "resolved", "search": "no_match"}
                    for index in sorted(search_no_match) if index not in resolved}
        resolved.update(no_match)
        mismatched = {index: {"status": "resolved", "source_read": "digest_mismatch_no_content"}
                      for index in sorted(digest_mismatch) if index not in resolved}
        resolved.update(mismatched)
        inspected = {index: {"status": "resolved", "inspect_shell": "closed_exit_no_effect"}
                     for index in sorted(inspect_failures) if index not in resolved}
        resolved.update(inspected)
        unread = {index: {"status": "resolved", "source_read": "failed_without_content"}
                  for index in sorted(no_content_reads) if index not in resolved}
        resolved.update(unread)
        for command in commands:
            if command["event_index"] in resolved:
                command["failure_resolution"] = resolved[command["event_index"]]
            elif command["event_index"] in verifier_recoveries:
                command["verifier_recovery_observation"] = verifier_recoveries[command["event_index"]]
            if command["event_index"] in non_attempts:
                command["execution_proof"] = "not_attempted"
                command["pre_execution_rejection"] = non_attempts[command["event_index"]]
        if _file(run / "terminal.json", caller.MAX_TERMINAL_BYTES) != terminal_raw:
            raise InspectionError("TERMINAL_CHANGED")
        result["counts"] = {"events": event_count, "commands": total_commands,
                            "file_changes": file_count,
                            "failed": evidence["failed_count"], "indexed": len(records),
                            "index_complete": evidence["complete"]}
        result["artifact_integrity"] = "verified"
        known_failure = evidence["failed_count"] or any(c["execution_proof"] == "known_failure" for c in commands)
        result["command_success"] = ("known_success" if commands and evidence["complete"] and
                                     all(c["execution_proof"] == "known_success" for c in commands)
                                     else "known_failure" if known_failure else "unproven")
        unresolved = (evidence["failed_count"] - len(raw_failed_indices)
                      + len(failed_indices - resolved.keys() - non_attempts.keys()))
        actual_success = (any(c["execution_proof"] == "known_success" for c in commands)
                          or (file_count > 0 and result["file_change_success"] == "known_success"))
        effective_success = ("known_success" if commands and evidence["complete"] and all(
            c["execution_proof"] == "known_success" or c["event_index"] in resolved
            or c["event_index"] in non_attempts for c in commands) and (not non_attempts or actual_success)
            else "known_failure" if unresolved else "unproven")
        if (known_failure and (not non_attempts or verifier_failures)
                and (verifier_failures or failed_indices - no_match.keys() - mismatched.keys()
                     - inspected.keys() - unread.keys() - non_attempts.keys())):
            result["verifier_failure_resolution"] = {
                "schema_version": "nokiy_focused_verifier_resolution_v1",
                "resolved_count": verifier_resolved_count, "unresolved_count": unresolved,
                "effective_command_success": effective_success,
            }
        if read_failure_count:
            result["source_read_failure_resolution"] = {
                "schema_version": "nokiy_source_read_failure_resolution_v1",
                "resolved_count": len(read_resolved), "unresolved_count": unresolved,
                "effective_command_success": effective_success,
            }
        if source_non_attempts:
            result["source_read_rejection_reconciliation"] = {
                "schema_version": "nokiy_source_read_rejection_reconciliation_v1",
                "not_attempted_count": len(source_non_attempts), "unresolved_count": unresolved,
                "effective_command_success": effective_success,
            }
        if refusals:
            result["jspace_refusal_reconciliation"] = {
                "schema_version": "nokiy_jspace_refusal_reconciliation_v1",
                "not_attempted_count": len(refusals), "unresolved_count": unresolved,
                "effective_command_success": effective_success,
            }
        if no_match:
            result["search_no_match_reconciliation"] = {
                "schema_version": "nokiy_search_no_match_reconciliation_v1",
                "no_match_count": len(no_match), "unresolved_count": unresolved,
                "effective_command_success": effective_success,
            }
        if mismatched:
            result["source_read_digest_mismatch_reconciliation"] = {
                "schema_version": "nokiy_source_read_digest_mismatch_reconciliation_v1",
                "mismatch_count": len(mismatched), "unresolved_count": unresolved,
                "effective_command_success": effective_success,
            }
        if inspected:
            result["inspect_shell_failure_reconciliation"] = {
                "schema_version": "nokiy_inspect_shell_failure_reconciliation_v1",
                "failure_count": len(inspected), "unresolved_count": unresolved,
                "effective_command_success": effective_success,
            }
        if unread:
            result["source_read_no_content_reconciliation"] = {
                "schema_version": "nokiy_source_read_no_content_reconciliation_v1",
                "failure_count": len(unread), "unresolved_count": unresolved,
                "effective_command_success": effective_success,
            }
        result["source_read_efficiency"] = reads.summary(
            evidence["complete"], result["command_success"] == "known_success")
        blocker = ("EVIDENCE_INDEX_PARTIAL" if not evidence["complete"] else
                   "COMMAND_FAILURE" if unresolved else
                    "EXECUTION_PROOF_UNAVAILABLE" if total_commands and effective_success != "known_success" else
                   file_blocker if file_blocker else
                    "EXECUTION_PROOF_UNAVAILABLE" if not total_commands and result["file_change_success"] != "known_success" else
                   "CLEANUP_UNPROVEN" if not cleanup_bound or not _cleanup_settled(result["cleanup"]) else
                   "TERMINAL_NOT_AVAILABLE" if terminal.get("status") != "RESULT_AVAILABLE" or detail is not None else None)
        if blocker is None and delivery.marker is not None and delivery.marker["terminal_status"] == "blocked":
            blocker = "WORKER_TERMINAL_EVIDENCE_BLOCKED"
        result["first_blocker"] = blocker
        result["status"] = "EVIDENCE_VERIFIED" if blocker is None else "INCOMPLETE_EVIDENCE"
        # This is execution settlement only. Preserve the original unavailable
        # proof, COMMAND_FAILURE, blocked delivery and all failed records above.
        denials = {index for index, command_id in postgate_denials.items()
                   if command_ids.get(command_id) == 1}
        if (not postgate_unindexed and known_failure
                and blocker == "COMMAND_FAILURE" and capability_request is not None
                and evidence["complete"] and len(commands) == total_commands and commands
                and all(row["event_index"] in postgate_receipts | denials | non_attempts.keys()
                        for row in commands)
                and file_blocker is None
                and (file_count == 0 or result["file_change_success"] == "known_success")
                and cleanup_bound and _cleanup_settled(result["cleanup"])
                and terminal.get("status") == "RESULT_AVAILABLE" and detail is None
                and type(terminal.get("runtime_exit_code")) is int and terminal["runtime_exit_code"] == 0
                and terminal.get("fallback_used") is False):
            try:
                if bound_contract is None:
                    bound_contract = _inspection_contract(run, terminal, capability_request,
                        result["artifacts"], require_artifact=False)
            except InspectionError:
                pass
            else:
                grants = bound_contract.get("verifier_commands")
                # V2 attests only completed workload-verifier failures, not
                # arbitrary settled commands or policy-denied non-attempts.
                focused_only = (not denials and isinstance(grants, list)
                    and raw_failed_indices == failed_indices == set(verifier_failures)
                    and len(verifier_failures) == evidence["failed_count"]
                    and all(verifier_calls[row["call_id"]] == 1
                            and rejection_calls.get(row["call_id"]) == 1
                            and row["verifier_index"] < len(grants)
                            for rows in verifier_failures.values() for row in rows))
                result["post_batch_verifier_settlement"] = {
                    "schema_version": ("nokiy_post_batch_verifier_settlement_v2" if focused_only
                                       else "nokiy_post_batch_verifier_settlement_v1"),
                    "terminal_sha256": result["terminal_sha256"],
                    "commands": total_commands, "failed": evidence["failed_count"],
                    "policy_denials": len(denials), "unsettled_commands": 0,
                }
        # Selection affects display only, after every indexed event and receipt
        # has been checked. It cannot hide a failure outside the selected commands.
        _display_commands(result, commands, selected, offset, command_stream=command_stream,
                          source_reads=source_reads)
        gap = _capability_gap(terminal, capability_request, result["terminal_sha256"],
                              verified_message_gap=message_gap)
        if gap is not None or message_gap is not None:
            # Explicit null preserves verified absence through caller projection;
            # it must not be replaced with an untrusted-preview diagnostic.
            result["capability_gap"] = gap
        if file_postimages is not None:
            # Optional closeout observations, not live source freshness or acceptance.
            result["file_changes"]["postimages"] = file_postimages
            if len(caller._canonical_bytes(result)) > MAX_PAGE_BYTES:
                result["file_changes"].pop("postimages")
        if message_answer is not None and result["status"] == "EVIDENCE_VERIFIED" and gap is None:
            # Reuse the checked final message, not a second artifact read. The
            # complete body is optional; it must never shorten an evidence page.
            result["parent_review_answer"] = {
                "schema_version": "nokiy_parent_review_answer_v1",
                "terminal_content_sha256": caller._canonical_sha256(terminal),
                "artifact": dict(message_ref), "text": message_answer,
                "parent_acceptance_required": True,
            }
            if len(caller._canonical_bytes(result)) > MAX_PAGE_BYTES:
                result.pop("parent_review_answer")
        if review is not None and result["status"] == "EVIDENCE_VERIFIED":
            context = review.project(terminal, verifier_calls)
            if context is not None:
                # Add last: recorded source bodies must never shorten an evidence page.
                result["parent_review_context"] = context
                if len(caller._canonical_bytes(result)) > MAX_PAGE_BYTES:
                    result.pop("parent_review_context")
        # Optional worker content comes from the completed, request-bound pass.
        # It never changes legacy handoff identity or command/check outcomes.
        reply_fields = handoff.reply_fields(terminal, delivery.marker, events.reference, command_ids)
        if reply_fields:
            result.setdefault("evidence_handoff", {
                "terminal_content_sha256": caller._canonical_sha256(terminal)}).update(reply_fields)
            _bound_terminal_reply(result["evidence_handoff"], result, MAX_PAGE_BYTES)
    except caller.EmbeddedNokiyError as exc:
        result["first_blocker"] = exc.code
    except UnicodeError:
        result["first_blocker"] = "TRAJECTORY_INVALID_JSON"
    except InspectionError as exc:
        result["status"] = "INCOMPLETE_EVIDENCE"
        result["first_blocker"] = exc.code
        if exc.diagnostic is not None:
            result["first_blocker_detail"] = exc.diagnostic
    return result


def _bound_terminal_reply(fields, container, budget):
    reply = fields.get("task_status_final_reply")
    if isinstance(reply, dict) and len(caller._canonical_bytes(container)) > budget:
        reply.pop("text", None)
        reply.update(complete=False, truncated=True, truncation_reason="PROJECTION_BUDGET")


def project_terminal(terminal: dict[str, Any], artifact_root: Path, request_id: str,
                     *, request_thread_id: str | None = None,
                     require_request_binding: bool = False) -> dict[str, Any]:
    """Attach offline evidence to CLI output only; never change the durable terminal.

    Inspection reads the raw terminal, not this projection. A missing caller binding
    cannot be recovered from the receipt or request data.
    """
    thread_id = os.environ.get("CODEX_THREAD_ID")
    evidence: dict[str, Any] = {
        "schema_version": "nokiy_result_inspection_v1", "status": "INCOMPLETE_EVIDENCE",
        "first_blocker": "CALLER_THREAD_ID_INVALID", "command_success": "unproven",
        "file_change_success": "unproven", "file_changes": None,
        "commands": [], "mission_acceptance": "parent_owned",
        "pagination": {"offset": 0, "limit": PAGE_SIZE, "next_offset": None, "stream": "priority"},
    }
    if isinstance(thread_id, str) and caller.THREAD_ID_PATTERN.fullmatch(thread_id):
        if require_request_binding and (not isinstance(request_thread_id, str)
                                        or not caller.THREAD_ID_PATTERN.fullmatch(request_thread_id)):
            evidence["first_blocker"] = "REQUEST_THREAD_ID_INVALID"
        elif request_thread_id is not None and request_thread_id != thread_id:
            evidence["first_blocker"] = "CALLER_THREAD_ID_MISMATCH"
        else:
            try:
                evidence = inspect(artifact_root, request_id, thread_id, command_stream="priority")
                for field in ("parent_review_context", "parent_review_answer"):
                    context = evidence.get(field)
                    if (context is not None and (not isinstance(context, dict)
                            or context.get("terminal_content_sha256") != caller._canonical_sha256(terminal))):
                        evidence.pop(field, None)
                # The original terminal already carries identity, usage and artifact
                # hashes. Keep one copy and use event_index to locate full evidence.
                for key in ("artifacts", "usage", "expected_thread_id", "request_id",
                            "terminal_status", "terminal_first_blocker"):
                    evidence.pop(key, None)
                if evidence.get("counts", {}).get("file_changes") == 0:
                    evidence.pop("file_changes", None)
                    evidence.pop("file_change_success", None)
                # Read-efficiency telemetry stays in the inspection pages, not every parent turn.
                evidence.pop("source_read_efficiency", None)
                evidence["commands"] = [_parent_command_row(row) for row in evidence["commands"]]
            except (OSError, ValueError, TypeError, KeyError, IndexError,
                    UnicodeError, RecursionError, OverflowError):
                evidence["first_blocker"] = "INSPECTION_UNAVAILABLE"
    handoff = evidence.pop("evidence_handoff", None)
    gap_inspected = "capability_gap" in evidence
    gap = evidence.pop("capability_gap", None)
    if gap is not None and "binding" in gap:
        binding = gap["binding"]
        if (binding["request_id"] != request_id or binding["native_thread_id"] != thread_id
                or binding["terminal_content_sha256"] != caller._canonical_sha256(terminal)):
            gap = _gap_diagnostic("CAPABILITY_GAP_TERMINAL_PROJECTION_MISMATCH")
    elif gap is None and not gap_inspected and _has_capability_gap(terminal):
        gap = _gap_diagnostic("CAPABILITY_GAP_UNTRUSTED_RESULT")
        gap["inspection_blocker"] = evidence["first_blocker"]
    projected = {**terminal, "result_inspection": evidence}
    # Only the current bound artifact verification may supply model-reported text.
    projected.pop("result_artifact_preview", None)
    projected.pop("task_status_final_reply", None)
    if (isinstance(handoff, dict)
            and handoff.get("terminal_content_sha256") == caller._canonical_sha256(terminal)
            and "task_status_final_reply" in handoff):
        projected["task_status_final_reply"] = handoff["task_status_final_reply"]
    if "result_artifact_source" in terminal:
        if (isinstance(handoff, dict)
                and handoff.get("terminal_content_sha256") == caller._canonical_sha256(terminal)):
            projected.update({key: handoff[key] for key in (
                "result_artifact", "result_artifact_source", "result_artifact_preview") if key in handoff})
        else:
            projected.update(full_core._EvidenceHandoff.unavailable("HANDOFF_EVIDENCE_UNAVAILABLE"))
    if "provider_failure_diagnostic" in projected:
        diagnostic = _inspected_failure_diagnostic(projected)
        if diagnostic is None:
            projected.pop("provider_failure_diagnostic")
        else:
            projected["provider_failure_diagnostic"] = diagnostic
    if gap is not None:
        projected["capability_gap"] = gap
    # Serial delivery appends retained-snapshot or read-only recovery metadata
    # after projection. Reserve its bounded size without publishing or rewriting it.
    delivery_metadata = {"inspection_summary": None, "inspection_summary_omission": "READ_ONLY_RECOVERY"}
    if require_request_binding:
        delivery_metadata = {"inspection_summary": {
            "path": str(artifact_root / request_id / INSPECTION_SUMMARY_FILE),
            "sha256": "0" * 64, "bytes": MAX_PROJECTED_BYTES},
            "inspection_summary_diagnostic": "x" * 64}
    projection_budget = MAX_PROJECTED_BYTES - (len(caller._canonical_bytes(delivery_metadata)) - 1)
    _bound_terminal_reply(projected, projected, projection_budget)
    if len(caller._canonical_bytes(projected)) > projection_budget:
        # Optional report transport must never displace evidence or add a blocker.
        projected.pop("result_artifact_preview", None)
    if len(caller._canonical_bytes(projected)) > projection_budget:
        # Discard source bodies before identities, commands, gaps or recovery evidence.
        evidence.pop("parent_review_context", None)
    if len(caller._canonical_bytes(projected)) > projection_budget:
        evidence.pop("parent_review_answer", None)
    file_changes = evidence.get("file_changes")
    if (isinstance(file_changes, dict) and "postimages" in file_changes
            and len(caller._canonical_bytes(projected)) > projection_budget):
        # Never displace command or handoff evidence for optional closeout identities.
        file_changes.pop("postimages")
    if gap is not None:
        if len(caller._canonical_bytes(projected)) > projection_budget:
            # Diagnostics never displace the first real blocker or execution evidence.
            projected["capability_gap"] = _gap_diagnostic("CAPABILITY_GAP_PROJECTION_TOO_LARGE")
    # A valid terminal is already bounded by MAX_TERMINAL_BYTES. Keep the
    # combined CLI response bounded even when inspection page sizes change.
    while evidence["commands"] and len(caller._canonical_bytes(projected)) > projection_budget:
        evidence["commands"].pop()
        evidence["pagination"]["next_offset"] = evidence["pagination"]["offset"] + len(evidence["commands"])
        evidence["projection_partial"] = True
        evidence["projection_blocker"] = "PROJECTION_PARTIAL"
        evidence["status"] = "INCOMPLETE_EVIDENCE"
        if evidence["first_blocker"] is None:
            evidence["first_blocker"] = "PROJECTION_PARTIAL"
    if len(caller._canonical_bytes(projected)) > projection_budget:
        projected["result_inspection"] = {
            "schema_version": "nokiy_result_inspection_v1", "status": "INCOMPLETE_EVIDENCE",
            "first_blocker": (evidence["first_blocker"] if evidence["first_blocker"] not in
                              (None, "PROJECTION_PARTIAL") else "PROJECTION_TOO_LARGE"),
            "command_success": evidence["command_success"],
            "file_change_success": evidence.get("file_change_success", "unproven"), "file_changes": None,
            "commands": [], "mission_acceptance": "parent_owned",
            "pagination": {**evidence["pagination"], "next_offset": 0},
            "projection_partial": True, "projection_blocker": "PROJECTION_TOO_LARGE",
            **{key: evidence[key] for key in ("terminal_sha256", "counts", "cleanup", "verifier_failure_resolution",
                                            "source_read_rejection_reconciliation", "source_read_failure_resolution",
                                            "search_no_match_reconciliation", "source_read_digest_mismatch_reconciliation",
                                            "inspect_shell_failure_reconciliation", "source_read_no_content_reconciliation",
                                            "jspace_refusal_reconciliation", "provider_failure_diagnostic_sha256")
               if key in evidence},
        }
    if "capability_gap" in projected and len(caller._canonical_bytes(projected)) > projection_budget:
        projected.pop("capability_gap")
    return projected


def _inspected_failure_diagnostic(projected: dict[str, Any]) -> dict[str, Any] | None:
    diagnostic = full_core.valid_provider_failure_diagnostic(projected.get("provider_failure_diagnostic"), projected)
    inspection = projected.get("result_inspection")
    if (diagnostic is not None and isinstance(inspection, dict)
            and inspection.get("provider_failure_diagnostic_sha256") == caller._canonical_sha256(diagnostic)):
        return diagnostic
    return None


def summarize_terminal(projected: dict[str, Any], artifact_root: Path,
                       request_id: str) -> dict[str, Any]:
    """Select return fields from an already projected terminal; do not inspect again.

    Runtime diagnostics are historical, not quality, provider-speed, billing or fresh-authority proof.
    Phase timings and usage coverage stay in terminal.json: every parent turn re-reads this
    summary, so it carries usage totals only.
    """
    fields = (
        "status", "first_typed_blocker", "first_blocker", "request_id", "request_sha256",
        "native_thread_id", "execution_id", "session_id", "model", "model_route",
        "requested_model", "observed_model", "model_provider",
        "reasoning_effort", "requested_reasoning_effort", "observed_reasoning_effort",
        "service_tier", "requested_service_tier", "observed_service_tier",
        "requested_model_provider", "observed_model_provider", "result_text",
        "requested_terminal_delivery", "observed_terminal_delivery", "terminal_evidence",
        "result_truncated", "result_artifact", "result_artifact_source", "result_artifact_preview",
        "task_status_final_reply",
        "wall_time_seconds", "tool_loop",
        "runtime_exit_code", "cleanup_pass", "continuation_owner", "mission_acceptance",
        "authority_effect", "fallback_used", "result_inspection", "capability_gap",
        "inspection_summary", "inspection_summary_diagnostic", "inspection_summary_omission",
    )
    inspection = projected["result_inspection"]
    summary = {
        "schema_version": "nokiy_terminal_summary_v1",
        "terminal_schema_version": projected.get("schema_version"),
        **{key: projected[key] for key in fields if key in projected},
        "terminal_path": str(artifact_root / request_id / "terminal.json"),
        # Only inspection can establish this hash. Never hash the projected response.
        "terminal_sha256": inspection.get("terminal_sha256"),
    }
    if "usage" in projected:
        usage = projected["usage"]
        summary["usage"] = ({key: usage[key] for key in SUMMARY_USAGE_FIELDS if key in usage}
                            if isinstance(usage, dict) else usage)
    if "provider_failure_diagnostic" in projected:
        diagnostic = _inspected_failure_diagnostic(projected)
        if diagnostic is not None:
            summary["provider_failure_diagnostic"] = diagnostic
    diagnostic = projected.get("startup_failure_diagnostic")
    if isinstance(diagnostic, dict) and diagnostic == inspection.get("startup_failure_diagnostic"):
        summary["startup_failure_diagnostic"] = diagnostic
    answer = inspection.get("parent_review_answer")
    artifact = projected.get("result_artifact")
    if (projected.get("status") == "RESULT_AVAILABLE" and projected.get("cleanup_pass") is True
            and projected.get("first_typed_blocker") is None and projected.get("first_blocker") is None
            and projected.get("request_id") == request_id
            and projected.get("request_sha256") == request_id[-64:]
            and type(projected.get("native_thread_id")) is str
            and caller.THREAD_ID_PATTERN.fullmatch(projected["native_thread_id"])
            and inspection.get("request_id", request_id) == request_id
            and inspection.get("expected_thread_id", projected["native_thread_id"]) == projected["native_thread_id"]
            and projected.get("result_truncated") is True and projected.get("capability_gap") is None
            and inspection.get("status") == "EVIDENCE_VERIFIED" and inspection.get("first_blocker") is None
            and inspection.get("projection_partial") is not True
            and inspection.get("artifact_integrity") == "verified"
            and type(answer) is dict and answer.get("schema_version") == "nokiy_parent_review_answer_v1"
            and answer.get("parent_acceptance_required") is True
            and type(artifact) is dict and answer.get("artifact") == artifact
            and type(answer.get("text")) is str and type(artifact.get("bytes")) is int
            and type(artifact.get("sha256")) is str and _SHA.fullmatch(artifact["sha256"])
            and type(answer.get("terminal_content_sha256")) is str
            and _SHA.fullmatch(answer["terminal_content_sha256"])):
        try:
            body = answer["text"].encode("utf-8")
        except UnicodeError:
            body = b""
        if (body and len(body) == artifact["bytes"]
                and hashlib.sha256(body).hexdigest() == artifact["sha256"]):
            # The original preview stays in terminal.json. Deliver its already
            # checked complete answer once, without changing raw truncation facts.
            summary["result_text"] = ""
            summary["result_text_omission"] = "VERIFIED_PARENT_REVIEW_ANSWER"
    return summary


def _summary_run(artifact_root: Path, request_id: str) -> Path:
    if (not isinstance(artifact_root, Path) or artifact_root.anchor != "/"
            or type(request_id) is not str or caller.REQUEST_ID_PATTERN.fullmatch(request_id) is None):
        raise InspectionError("INSPECTION_SUMMARY_PATH_INVALID")
    run = artifact_root / request_id
    spelling = str(run / INSPECTION_SUMMARY_FILE)
    if (len(spelling.encode("utf-8")) > 1024
            or any(part in (".", "..") or part != part.strip() or part.startswith("~")
                   or part.casefold() == ".git"
                   or caller.REQUEST_ID_PATTERN.fullmatch(part) and part != request_id
                   for part in run.parts[1:])
            or any(char in spelling for char in "\\:*?[]{}")
            or any(ord(char) < 32 or ord(char) == 127 for char in spelling)):
        raise InspectionError("INSPECTION_SUMMARY_PATH_INVALID")
    return run


@contextmanager
def _summary_directory(run: Path):
    """Pin every physical parent, not just the final directory, without resolving aliases."""
    descriptor = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(run.anchor, flags)
        for part in run.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _summary_identity(summary: dict[str, Any], run: Path, request_id: str,
                      expected_thread_id: str, terminal_sha256: str) -> None:
    inspection = summary.get("result_inspection")
    if (type(expected_thread_id) is not str or caller.THREAD_ID_PATTERN.fullmatch(expected_thread_id) is None
            or type(terminal_sha256) is not str or _SHA.fullmatch(terminal_sha256) is None
            or summary.get("schema_version") != "nokiy_terminal_summary_v1"
            or summary.get("terminal_schema_version") != caller.TERMINAL_SCHEMA_VERSION
            or summary.get("request_id") != request_id or summary.get("request_sha256") != request_id[-64:]
            or summary.get("native_thread_id") != expected_thread_id
            or summary.get("terminal_path") != str(run / "terminal.json")
            or summary.get("terminal_sha256") != terminal_sha256
            or type(inspection) is not dict or inspection.get("schema_version") != "nokiy_result_inspection_v1"
            or inspection.get("terminal_sha256") != terminal_sha256
            or inspection.get("status") not in ("EVIDENCE_VERIFIED", "INCOMPLETE_EVIDENCE")
            or inspection.get("mission_acceptance") != "parent_owned"
            or any(key in inspection and inspection[key] != expected for key, expected in
                   (("request_id", request_id), ("expected_thread_id", expected_thread_id)))):
        raise InspectionError("INSPECTION_SUMMARY_IDENTITY_MISMATCH")


def publish_inspection_summary(projected: dict[str, Any], artifact_root: Path,
                               request_id: str, *, expected_thread_id: str) -> dict[str, Any]:
    """Create one bounded historical snapshot from the computed projection, never inspect.

    Return a path/SHA/bytes reference. Identical existing bytes may be reused;
    conflicting or interrupted publications are never overwritten or repaired here.
    This is not live ownership, permission, process clearance or acceptance proof.
    """
    run = _summary_run(artifact_root, request_id)
    summary = summarize_terminal(projected, artifact_root, request_id)
    # A snapshot cannot recursively refer to itself or retain a previous delivery diagnostic.
    for key in ("inspection_summary", "inspection_summary_diagnostic", "inspection_summary_omission"):
        summary.pop(key, None)
    _summary_identity(summary, run, request_id, expected_thread_id, summary.get("terminal_sha256"))
    raw = caller._canonical_bytes(summary) + b"\n"
    if len(raw) > MAX_PROJECTED_BYTES:
        raise InspectionError("INSPECTION_SUMMARY_TOO_LARGE")
    with _summary_directory(run) as descriptor:
        try:
            caller._write_create_only(Path(INSPECTION_SUMMARY_FILE), summary, dir_fd=descriptor)
        except FileExistsError:
            if _file(Path(INSPECTION_SUMMARY_FILE), MAX_PROJECTED_BYTES, dir_fd=descriptor) != raw:
                raise InspectionError("INSPECTION_SUMMARY_COLLISION")
        else:
            if _file(Path(INSPECTION_SUMMARY_FILE), MAX_PROJECTED_BYTES, dir_fd=descriptor) != raw:
                raise InspectionError("INSPECTION_SUMMARY_CHANGED")
    return {"path": str(run / INSPECTION_SUMMARY_FILE), "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw)}


def retain_inspection_summary(projected: dict[str, Any], artifact_root: Path,
                              request_id: str, *, expected_thread_id: str) -> dict[str, Any]:
    """Delivery-only publication; a failure cannot replace terminal/evidence status or replay."""
    try:
        reference = publish_inspection_summary(projected, artifact_root, request_id,
                                               expected_thread_id=expected_thread_id)
    except Exception as error:
        # Never expose unbounded exception text or replace the first execution blocker.
        diagnostic = (error.code[:64] if isinstance(error, InspectionError) and type(error.code) is str
                      else "INSPECTION_SUMMARY_PUBLICATION_FAILED")
        return {**projected, "inspection_summary": None,
                "inspection_summary_diagnostic": diagnostic}
    return {**projected, "inspection_summary": reference}


def load_inspection_summary(reference: dict[str, Any], *, expected_request_id: str,
                            expected_thread_id: str,
                            terminal_reference: dict[str, str]) -> dict[str, Any]:
    """Load only the bounded SHA-bound snapshot; no terminal/trajectory/process reinspection.

    Both references and expected identities come from the parent's original delivery,
    not from the file being loaded. The result is historical evidence, not fresh clearance.
    """
    if (type(reference) is not dict or set(reference) != {"path", "sha256", "bytes"}
            or type(reference["path"]) is not str or type(reference["sha256"]) is not str
            or len(reference["path"].encode("utf-8")) > 1024
            or _SHA.fullmatch(reference["sha256"]) is None
            or type(reference["bytes"]) is not int or not 1 <= reference["bytes"] <= MAX_PROJECTED_BYTES
            or type(terminal_reference) is not dict or set(terminal_reference) != {"path", "sha256"}
            or type(terminal_reference["path"]) is not str or type(terminal_reference["sha256"]) is not str
            or len(terminal_reference["path"].encode("utf-8")) > 1024
            or _SHA.fullmatch(terminal_reference["sha256"]) is None):
        raise InspectionError("INSPECTION_SUMMARY_REFERENCE_INVALID")
    run = _summary_run(Path(terminal_reference["path"]).parent.parent, expected_request_id)
    if (terminal_reference["path"] != str(run / "terminal.json")
            or reference["path"] != str(run / INSPECTION_SUMMARY_FILE)):
        raise InspectionError("INSPECTION_SUMMARY_PATH_INVALID")
    with _summary_directory(run) as descriptor:
        raw = _file(Path(INSPECTION_SUMMARY_FILE), MAX_PROJECTED_BYTES, dir_fd=descriptor)
    if len(raw) != reference["bytes"] or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise InspectionError("INSPECTION_SUMMARY_HASH_MISMATCH")
    summary = _json(raw)
    _summary_identity(summary, run, expected_request_id, expected_thread_id, terminal_reference["sha256"])
    return summary


def parent_review_record(projected: dict[str, Any], *, expected_request_id: str,
                         expected_thread_id: str, terminal_reference: dict[str, str],
                         decision: str, task_evidence: list[dict[str, str]],
                         note: str) -> dict[str, Any]:
    """Build a parent's declaration from retained evidence, without inspecting again.

    terminal_reference is the original publication's path/SHA, never a hash of
    the projection. All task_evidence entries are necessary parent-supplied checks
    (at most eight normalized absolute path/SHA/check_status references, including
    task artifacts outside the workspace). The parent owns their completeness,
    provenance, freshness and semantic interpretation.
    Negative/pending decisions may retain failed execution evidence, but still
    require verified artifact integrity and proven cleanup. No record proves its
    inputs independently or grants deployment, permissions, or a lease.
    """
    def text(value: object, limit: int, *, nullable: bool = False) -> str | None:
        if value is None and nullable:
            return None
        if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > limit:
            raise ValueError("Parent review text invalid")
        return value

    def sha(value: object) -> str:
        if type(value) is not str or _SHA.fullmatch(value) is None:
            raise ValueError("Parent review SHA invalid")
        return value

    def path(value: object) -> str:
        value = text(value, 1024)
        if not value.startswith("/"):
            raise ValueError("Parent review path scope invalid")
        parts = value.split("/")[1:]
        if (any(not part or part in (".", "..") or part != part.strip()
                or part.startswith("~") or part.casefold() == ".git" for part in parts)
                or any(char in value for char in "\\:*?[]{}")
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or any(caller.REQUEST_ID_PATTERN.fullmatch(part) and part != expected_request_id
                       for part in parts)):
            raise ValueError("Parent review path invalid or foreign")
        return value

    if (type(projected) is not dict or type(expected_request_id) is not str
            or caller.REQUEST_ID_PATTERN.fullmatch(expected_request_id) is None
            or type(expected_thread_id) is not str
            or caller.THREAD_ID_PATTERN.fullmatch(expected_thread_id) is None
            or type(decision) is not str or decision not in ("accepted", "rejected", "pending")):
        raise ValueError("Parent review input or explicit decision invalid")
    schema = projected.get("schema_version")
    if (schema not in (caller.TERMINAL_SCHEMA_VERSION, "nokiy_terminal_summary_v1")
            or schema == "nokiy_terminal_summary_v1"
            and projected.get("terminal_schema_version") != caller.TERMINAL_SCHEMA_VERSION):
        raise ValueError("Parent review terminal schema invalid")
    if (projected.get("request_id") != expected_request_id
            or projected.get("request_sha256") != expected_request_id[-64:]
            or projected.get("native_thread_id") != expected_thread_id):
        raise ValueError("Parent review request/thread mismatch")
    inspection = projected.get("result_inspection")
    if (type(inspection) is not dict or inspection.get("schema_version") != "nokiy_result_inspection_v1"
            or inspection.get("status") not in ("EVIDENCE_VERIFIED", "INCOMPLETE_EVIDENCE")
            or inspection.get("artifact_integrity") != "verified"
            or inspection.get("mission_acceptance") != "parent_owned"):
        raise ValueError("Parent review inspection unverified")
    for key, expected in (("request_id", expected_request_id), ("expected_thread_id", expected_thread_id)):
        # CLI projection deliberately removes these redundant inspection fields.
        if key in inspection and inspection[key] != expected:
            raise ValueError("Parent review inspection identity mismatch")
    if type(terminal_reference) is not dict or set(terminal_reference) != {"path", "sha256"}:
        raise ValueError("Parent review original terminal reference invalid")
    terminal_path = path(terminal_reference["path"])
    terminal_sha = sha(terminal_reference["sha256"])
    if (not terminal_path.endswith("/" + expected_request_id + "/terminal.json")
            or sha(inspection.get("terminal_sha256")) != terminal_sha
            or "terminal_sha256" in projected and projected["terminal_sha256"] != terminal_sha
            or "terminal_path" in projected and projected["terminal_path"] != terminal_path
            or schema == "nokiy_terminal_summary_v1"
            and ("terminal_sha256" not in projected or "terminal_path" not in projected)):
        raise ValueError("Parent review original terminal/hash mismatch")
    cleanup = inspection.get("cleanup")
    if (projected.get("cleanup_pass") is not True or type(cleanup) is not dict
            or not _cleanup_settled(cleanup)):
        raise ValueError("Parent review cleanup unproven")
    blockers = {
        "terminal": text(projected.get("first_typed_blocker"), 256, nullable=True),
        "terminal_detail": text(projected.get("first_blocker"), 256, nullable=True),
        "inspection": text(inspection.get("first_blocker"), 256, nullable=True),
        "projection": text(inspection.get("projection_blocker"), 256, nullable=True),
    }
    scope = {"kind": "parent_declaration_not_independent_proof", "mission_acceptance": "parent_owned",
             "authority_effect": "none", "permission_grant": False, "deployment_or_lease_grant": False}
    for key in ("continuation_owner", "authority_effect"):
        if key in projected:
            scope["terminal_" + key] = text(projected[key], 64)
    if "projection_partial" in inspection:
        if type(inspection["projection_partial"]) is not bool:
            raise ValueError("Parent review projection flag invalid")
        scope["projection_partial"] = inspection["projection_partial"]
    gap = projected.get("capability_gap")
    if gap is not None:
        if type(gap) is not dict:
            raise ValueError("Parent review capability diagnostic invalid")
        scope["capability_gap_status"] = text(gap.get("status"), 64)
        scope["requires_parent_readmission"] = True
        blockers["capability_gap"] = text(gap.get("first_blocker"), 256, nullable=True)
    if type(task_evidence) is not list or len(task_evidence) > 8:
        raise ValueError("Parent review task references invalid or excessive")
    references = []
    seen = set()
    for reference in task_evidence:
        if (type(reference) is not dict or set(reference) != {"path", "sha256", "check_status"}
                or type(reference["check_status"]) is not str
                or reference["check_status"] not in ("passed", "failed", "unknown")):
            raise ValueError("Parent review task reference malformed")
        evidence_path = path(reference["path"])
        if evidence_path in seen:
            raise ValueError("Parent review task reference duplicated")
        seen.add(evidence_path)
        references.append({"path": evidence_path, "sha256": sha(reference["sha256"]),
                           "check_status": reference["check_status"]})
    status = projected.get("status")
    if status not in ("RESULT_AVAILABLE", "BLOCKED"):
        raise ValueError("Parent review terminal not completed")
    if decision == "accepted" and (inspection["status"] != "EVIDENCE_VERIFIED"
            or status != "RESULT_AVAILABLE" or any(blockers.values()) or gap is not None
            or scope.get("projection_partial") is True or not references
            or any(reference["check_status"] != "passed" for reference in references)):
        raise ValueError("Parent review acceptance requires complete successful checks")
    return {"schema_version": "nokiy_parent_review_record_v1", "decision": decision,
            "request_id": expected_request_id, "request_sha256": projected["request_sha256"],
            "native_thread_id": expected_thread_id,
            "terminal_reference": {"path": terminal_path, "sha256": terminal_sha},
            "terminal_status": status, "inspection_status": inspection["status"],
            "cleanup_proven": True, "blockers": blockers, "scope": scope,
            "task_evidence": references, "note": text(note, MAX_DIAGNOSTIC_BYTES)}


def _review_path(value: object, request_id: str | None = None) -> Path:
    if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > 1024:
        raise InspectionError("PARENT_REVIEW_PATH_INVALID")
    parsed = Path(value)
    if (parsed.anchor != "/" or str(parsed) != value or not parsed.name
            or any(part in (".", "..") or part != part.strip() or part.startswith("~")
                   or part.casefold() == ".git"
                   or request_id is not None and caller.REQUEST_ID_PATTERN.fullmatch(part)
                   and part != request_id for part in parsed.parts[1:])
            or any(char in value for char in "\\:*?[]{}")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise InspectionError("PARENT_REVIEW_PATH_INVALID")
    return parsed


def _review_sha256(path: Path) -> str:
    """Hash a regular file through pinned, no-follow parents, with bounded memory."""
    with _summary_directory(path.parent) as directory:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                             dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise InspectionError("UNSAFE_FILE")
            digest = hashlib.sha256()
            for raw in iter(lambda: stream.read(65536), b""):
                digest.update(raw)
            after = os.fstat(stream.fileno())
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if (full_core._artifact_stamp(before) != full_core._artifact_stamp(after)
                    or full_core._artifact_stamp(before) != full_core._artifact_stamp(current)):
                raise InspectionError("PARENT_REVIEW_EVIDENCE_CHANGED")
    return digest.hexdigest()


def declare_task_review(packet: dict[str, Any], review_path: str) -> dict[str, Any]:
    """Validate a parent-authored packet, then exclusively create its admitted record.

    This hashes only supplied task evidence/postimages and loads the retained summary;
    it never reinspects execution or infers semantic acceptance. The parent still owns
    the complete requirements, review, provenance, output admission and final acceptance.
    """
    fields = {"schema_version", "summary_reference", "request_id", "thread_id",
              "terminal_reference", "required_check_paths", "task_checks",
              "expected_postimages", "review_complete", "semantic_decision", "note"}
    if (type(packet) is not dict or set(packet) != fields
            or type(packet["schema_version"]) is not str
            or packet["schema_version"] != PARENT_REVIEW_INPUT_SCHEMA
            or type(packet["request_id"]) is not str
            or caller.REQUEST_ID_PATTERN.fullmatch(packet["request_id"]) is None
            or type(packet["thread_id"]) is not str
            or caller.THREAD_ID_PATTERN.fullmatch(packet["thread_id"]) is None
            or type(packet["review_complete"]) is not bool
            or packet["semantic_decision"] is not None and (
                type(packet["semantic_decision"]) is not str
                or packet["semantic_decision"] not in ("accepted", "rejected", "pending"))
            or type(packet["required_check_paths"]) is not list
            or type(packet["task_checks"]) is not list
            or len(packet["task_checks"]) > 8
            or type(packet["expected_postimages"]) is not dict
            or len(packet["required_check_paths"]) + len(packet["expected_postimages"]) > 8):
        raise InspectionError("PARENT_REVIEW_INPUT_INVALID")
    request_id, thread_id = packet["request_id"], packet["thread_id"]
    output = _review_path(review_path, request_id)
    required = [str(_review_path(path, request_id)) for path in packet["required_check_paths"]]
    if len(set(required)) != len(required):
        raise InspectionError("PARENT_REVIEW_REQUIRED_CHECK_DUPLICATED")
    postimages = [{"path": str(_review_path(path, request_id)), "sha256": sha,
                   "check_status": "passed"} for path, sha in packet["expected_postimages"].items()]
    summary = load_inspection_summary(packet["summary_reference"], expected_request_id=request_id,
                                     expected_thread_id=thread_id,
                                     terminal_reference=packet["terminal_reference"])
    # Check only the original terminal's file kind, not its contents or execution again.
    with _summary_directory(Path(packet["terminal_reference"]["path"]).parent) as directory:
        if not stat.S_ISREG(os.stat("terminal.json", dir_fd=directory, follow_symlinks=False).st_mode):
            raise InspectionError("UNSAFE_FILE")
    bindings = {"expected_request_id": request_id, "expected_thread_id": thread_id,
                "terminal_reference": packet["terminal_reference"], "note": packet["note"]}
    # Reuse the existing builder for reference, identity, integrity and cleanup validation.
    checked = parent_review_record(summary, **bindings, decision="pending",
                                   task_evidence=packet["task_checks"] + postimages)["task_evidence"]
    supplied = {check["path"] for check in checked[:len(packet["task_checks"])]}
    if (not supplied <= set(required)
            or str(output) in {*required, *(check["path"] for check in checked),
                               packet["summary_reference"]["path"], packet["terminal_reference"]["path"]}):
        raise InspectionError("PARENT_REVIEW_EVIDENCE_SCOPE_INVALID")
    decision = packet["semantic_decision"] or "pending"
    if decision == "accepted" and (not packet["review_complete"] or supplied != set(required)):
        decision = "pending"
    for check in checked:
        if _review_sha256(Path(check["path"])) != check["sha256"]:
            raise InspectionError("PARENT_REVIEW_HASH_MISMATCH")
    record = parent_review_record(summary, **bindings, decision=decision, task_evidence=checked)
    raw = caller._canonical_bytes(record) + b"\n"
    with _summary_directory(output.parent) as directory:
        descriptor = os.open(output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        identity = None
        try:
            identity = os.fstat(descriptor)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # Remove only our own interrupted creation, never an existing/replaced output.
            try:
                current = os.stat(output.name, dir_fd=directory, follow_symlinks=False)
                if identity is not None and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    os.unlink(output.name, dir_fd=directory)
            except OSError:
                pass
            raise
        finally:
            if descriptor != -1:
                os.close(descriptor)
    return record


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["parent-review"]:
        parser = argparse.ArgumentParser(description="Declare a parent-owned review without reinspection")
        parser.add_argument("--input", required=True, help="Exact admitted parent-authored JSON packet")
        parser.add_argument("--output", required=True, help="New normalized absolute admitted record path")
        args = parser.parse_args(argv[1:])
        try:
            source = _review_path(args.input)
            with _summary_directory(source.parent) as directory:
                packet = _json(_file(Path(source.name), MAX_PROJECTED_BYTES, dir_fd=directory))
            thread_id = os.environ.get("CODEX_THREAD_ID")
            if (type(thread_id) is not str or caller.THREAD_ID_PATTERN.fullmatch(thread_id) is None
                    or packet.get("thread_id") != thread_id):
                raise InspectionError("PARENT_REVIEW_CALLING_THREAD_MISMATCH")
            result = declare_task_review(packet, args.output)
        except (ValueError, OSError, caller.EmbeddedNokiyError) as error:
            code = error.code if isinstance(error, InspectionError) else "PARENT_REVIEW_DECLARATION_FAILED"
            print(json.dumps({"schema_version": "nokiy_parent_review_error_v1", "error": code},
                             sort_keys=True, separators=(",", ":")))
            return 2
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Parent declaration: parent-review --input <parent-authored JSON> --output <new exact record path>"))
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--expected-thread-id", required=True)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--command-stream", choices=("history", "priority"), default="history",
                        help="Offset stream; priority omits only proven successful typed source-read-only commands")
    parser.add_argument("--command-sha256", action="append", default=[],
                        help="Exact UTF-8 command SHA-256 to display; all evidence is still verified")
    args = parser.parse_args(argv)
    result = inspect(args.artifact_root, args.request_id, args.expected_thread_id,
                     offset=args.offset, command_sha256=tuple(args.command_sha256),
                     command_stream=args.command_stream)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "EVIDENCE_VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
