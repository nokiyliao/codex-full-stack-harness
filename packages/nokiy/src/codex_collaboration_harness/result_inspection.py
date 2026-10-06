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
import stat
from typing import Any

from . import embedded_nokiy as caller
from . import full_core
from . import file_change_evidence
from . import postimage_context
from .verifier_parent import VERIFICATION_EVIDENCE_SCHEMA, _verification_targets

MAX_JSON = 2 * 1024 * 1024
PAGE_SIZE = 16
MAX_SELECTED_COMMANDS = 8
MAX_PAGE_BYTES = 32768
MAX_COMMAND_BYTES = 2048
MAX_DIAGNOSTIC_BYTES = 512
MAX_PROJECTED_BYTES = 64 * 1024
MAX_REVIEW_CONTEXT_BYTES = 12 * 1024
INSPECTION_SUMMARY_FILE = "inspection-summary.json"
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
    def __init__(self, code: str):
        self.code = code
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


def _receipt(run: Path, output: dict[str, Any]) -> str:
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
    for receipt in (actual, value):
        code = receipt.get("exit_code")
        if (receipt.get("schema_version") != "tura_command_terminal_receipt_v1"
                or receipt.get("outcome") != "known" or type(code) is not int
                or receipt.get("terminal_state") != ("completed" if code == 0 else "failed")
                or any(receipt.get(key) is not True for key in
                       ("process_reaped", "process_group_empty", "termination_proven"))):
            raise InspectionError("RECEIPT_PROOF_INVALID")
    return "matched"


def _compact_utf8_json(value: object) -> bytes:
    # The router's command/witness identities use UTF-8, not the caller's
    # ASCII-escaped request identity encoding.
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _source_read_arguments(line: str) -> tuple[dict[str, Any] | None, bool]:
    """Parse once, recognizing syntax and unambiguous named-argument type errors.

    Do not infer a rejection from diagnostics, paths, ranges, hashes or read
    permissions. The durable router witness still has to attest the rejection.
    Positional forms are not reconstructed from a projected observation.
    """
    try:
        args = _json(line.encode("utf-8"))
    except InspectionError as exc:
        # Recursion/budget errors and unsupported positional forms are not proof
        # of the router's typed parser rejection. Strict JSON errors are.
        return None, isinstance(exc.__cause__, (json.JSONDecodeError, caller.EmbeddedNokiyError))
    if "path" not in args or not isinstance(args["path"], str):
        return args, True
    for key in ("start_line", "end_line", "context_lines"):
        value = args.get(key)
        if value is not None and (type(value) is not int or not 0 <= value < 2**64):
            return args, True
    return args, ((args.get("line_numbers") is not None and type(args["line_numbers"]) is not bool)
                 or (args.get("expected_sha256") is not None and not isinstance(args["expected_sha256"], str))
                 or (args.get("search_terms") is not None and (
                     not isinstance(args["search_terms"], list)
                     or any(not isinstance(term, str) for term in args["search_terms"]))))


def _rejected_source_read_command(item: dict[str, Any]) -> dict[str, Any] | None:
    """Reconstruct one exact command, never guess omitted optional metadata."""
    kind, line = item.get("command_type"), item.get("command")
    if not isinstance(line, str) or len(line.encode("utf-8")) > 16384 or kind not in (None, "source_read", "command_run"):
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
    if not rejected:
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
    command = _rejected_source_read_command(item)
    scopes, allowed, denied = (contract.get("read_scopes"), contract.get("allowed_operations"),
                               contract.get("denied_operations", []))
    if (set(output) != {"pre_execution_rejection"} or not isinstance(value, dict) or set(value) != keys
            or command is None or item.get("status") != "failed" or item.get("exit_code") is not None
            or item.get("success", False) is not False
            or any(key in item for key in ("results", "changes", "file_changes", "postimage", "postimages",
                                           "source_postimages", "terminal_receipt", "terminal_receipt_path",
                                           "stdout", "stderr"))
            or value["schema_version"] != "nokiy_source_read_pre_execution_rejection_v1"
            or value["owner"] != "router" or value["rejection_kind"] != "json_syntax_or_shape"
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


def _checked_results(run: Path, output: dict[str, Any],
                     payload_receipts: list[str] | None = None) -> list[str]:
    """Check every batch result, including receipts of commands we will retain."""
    if "results" not in output:
        return []
    entries = output["results"]
    if not isinstance(entries, list) or not entries:
        return ["unavailable"]
    proofs = []
    for entry in entries:
        if not isinstance(entry, dict):
            proofs.append("unavailable")
            if payload_receipts is not None:
                payload_receipts.append("unavailable")
            continue
        entry_receipt = _receipt(run, entry)
        payload = entry.get("output")
        if not isinstance(payload, dict):
            proofs.append("unavailable")
            if payload_receipts is not None:
                payload_receipts.append("unavailable")
            continue
        payload_receipt = _receipt(run, payload)
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
                        postimages: list[dict[str, Any]] | None) -> dict[str, dict] | None:
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
        row = initial.get(name) if isinstance(initial, dict) else None
        if (not isinstance(row, dict) or not file_change_evidence._sha(row.get("sha256"))
                or type(row.get("mode")) is not int or not 0 <= row["mode"] <= 0o7777
                or ("bytes" in row and (type(row["bytes"]) is not int
                    or not 0 <= row["bytes"] <= file_change_evidence.MAX_FILE_BYTES))):
            return None
        final[name] = {k: row[k] for k in ("sha256", "bytes", "mode") if k in row}
    return final


def _verification_matches(observation: dict[str, Any], contract: dict[str, Any],
                          sources: dict[str, dict]) -> bool:
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
            and all(isinstance(row, dict) and set(row) == {"sha256", "bytes", "mode"}
                    and file_change_evidence._sha(row["sha256"])
                    and type(row["bytes"]) is int and 0 <= row["bytes"] <= file_change_evidence.MAX_FILE_BYTES
                    and type(row["mode"]) is int and 0 <= row["mode"] <= 0o7777
                    and all(row[key] == expected for key, expected in sources[name].items())
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
                 request: dict[str, Any]) -> None:
        self.contract, self.postimages, self.request = contract, postimages, request
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
        self.ranges: dict[tuple[str, str, int, int], dict[str, Any]] = {}
        self.seen: dict[tuple[str, int], str] = {}
        self.totals: dict[str, int] = {}
        self.bytes = 0
        self.verifiers: list[dict[str, Any]] = []
        self.verifier_files: dict[str, tuple[str, dict, int]] = {}
        self.verifier_seen: dict[tuple[str, int], str] = {}
        self.verifier_calls: set[str] = set()
        self.verifier_bytes = 0
        self.invalid = False

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
            index: int, event_sha256: str, last_mutation_index: int) -> None:
        if self.invalid or proof != "known_success" or index <= last_mutation_index:
            return
        for part_index, part in enumerate(parts):
            payload = part[2]
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
                    or self.final.get(path) != sha):
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
        displayed = retained
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
        trajectory_ref = full_core._artifact_reference(run / "core.jsonl", terminal.get("trajectory_artifact"))
        events = full_core._Trajectory(run / "core.jsonl", reference=trajectory_ref)
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
                                  "no_live_descendants", "cleanup_error"))
                         and scope.get("engine_reaped") is True
                         and scope.get("no_live_descendants") is True
                         and scope.get("cleanup_error") is None)
        if isinstance(scope, dict):
            result["cleanup"]["engine_pid"] = _process(scope.get("engine_pid"))
            result["cleanup"]["supervisor_pid"] = _process(scope.get("supervisor_pid"))
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
            if terminal.get("status") == "RESULT_AVAILABLE":
                events.limit = trajectory_limit
        result_limit = original.get("max_result_bytes") if original is not None else caller.MAX_RESULT_BYTES
        if type(result_limit) is not int or not 256 <= result_limit <= caller.MAX_RESULT_BYTES:
            raise InspectionError("RESULT_BUDGET_INVALID")
        capability_request = original  # Retain the already checked binding, not later proof-local reads.
        turn = full_core._TurnSummary(expected_turn, require_completed=terminal.get("status") == "RESULT_AVAILABLE")
        delivery = full_core._TerminalEvidence(original if full_core_receipt else None)
        event_count = file_count = 0
        last_mutation_index = -1
        message_preview, message_truncated, message_ref = None, False, None
        message_gap = None
        def observations():
            nonlocal event_count, file_count, last_mutation_index
            nonlocal message_preview, message_truncated, message_ref, message_gap
            for event in events:
                event_count += 1
                delivery.observe(event)
                turn.observe(event)
                item = event.get("item")
                file_count += isinstance(item, dict) and item.get("type") == "file_change"
                if isinstance(item, dict) and item.get("type") == "file_change":
                    last_mutation_index = event_count - 1
                if (full_core_receipt and event.get("type") == "item.completed"
                        and isinstance(item, dict) and item.get("type") == "assistant_message"):
                    text = item.get("text", "")
                    if not isinstance(text, str):
                        raise InspectionError("NOKIY_FULL_CORE_RESULT_INVALID")
                    message_preview, message_truncated = full_core._text_preview(text, result_limit)
                    message_ref = (full_core._message_reference(run / "last-message.txt", text)
                                   if message_truncated else None)
                    # Overwrite even with absence/error: only the LAST completed
                    # message can supply a handoff, after all later integrity checks.
                    message_gap = (_gap_decode(text) if terminal.get("status") == "RESULT_AVAILABLE" else None)
                    del text
                yield event
        expected_evidence = full_core._command_evidence(observations())
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
            delivery.validate_projection(terminal)
            if (message_preview is None or message_preview != terminal.get("result_text")
                    or message_truncated is not terminal.get("result_truncated")
                    or message_ref != terminal.get("result_artifact")):
                raise InspectionError("NOKIY_FULL_CORE_RESULT_INVALID")
        if terminal.get("result_truncated") is True or terminal.get("result_artifact") is not None:
            _, _, result_ref = full_core._result_projection(
                run, terminal.get("result_text"), terminal.get("result_truncated"), terminal.get("result_artifact"), result_limit)
            result["artifacts"]["result"] = {"path": "last-message.txt", "sha256": result_ref["sha256"],
                                              "bytes": result_ref["bytes"]}
        if terminal.get("status") == "BLOCKED" and terminal.get("command_evidence_artifact") is None:
            # No replay and no terminal rewrite. Only settled, request-local DB
            # observations can supplement a failed execution's missing projection.
            if (not cleanup_bound or result["cleanup"]["historical_pass"] is not True
                    or any(result["cleanup"][k] != "absent" for k in ("engine_pid", "supervisor_pid"))):
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
            _display_commands(result, commands, selected, offset, command_stream=command_stream)
            return result
        data["command_evidence"], result["artifacts"]["command_evidence"] = _artifact(
            run, "command-evidence.json", terminal.get("command_evidence_artifact"), MAX_JSON)
        evidence = _json(data["command_evidence"])
        file_ref = terminal.get("file_change_evidence_artifact")
        file_summary = terminal.get("file_change_evidence_summary")
        file_blocker = None
        file_postimages = None
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
        review = (_ParentReviewContext(bound_contract, file_postimages, capability_request)
                  if capability_request is not None
                  and capability_request.get("terminal_delivery") == "evidence_only"
                  and bound_contract is not None and file_postimages is not None else None)
        failed_indices, raw_failed_indices = set(), set()
        verifier_failures, verifier_passes, verifier_calls = {}, [], {}
        rejection_candidates, rejection_calls = {}, {}
        by_index = {record["event_index"]: record for record in records}
        for index, event in enumerate(events):
            record = by_index.get(index)
            if record is None:
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
            receipt_state = _receipt(run, output)
            child_receipts: list[str] = []
            child_proofs = _checked_results(run, output, child_receipts)
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
                             "command_bytes": len(command.encode()),
                             "status": status[:32] if isinstance(status, str) else None,
                             "diagnostic_excerpt": diagnostic[:512] if isinstance(diagnostic, str) else None,
                             "exit_code": exit_code, "receipt_check": receipt_state,
                             "execution_proof": proof})
            if record["success"] is False or proof == "known_failure":
                failed_indices.add(index)
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
            parts = _typed_parts(item, output, receipt_state)
            if any(kind == "apply_patch" for kind, _, _ in parts):
                last_mutation_index = max(last_mutation_index, index)
                if review is not None:
                    review.reset()
            part_proofs = child_proofs if "results" in output else [proof]
            part_receipts = child_receipts if "results" in output else [receipt_state]
            eligible, failed_verifiers = bool(parts) and len(parts) == len(part_proofs), []
            for part_index, ((kind, line, payload), part_proof, part_receipt) in enumerate(
                    zip(parts, part_proofs, part_receipts)):
                observation = (_parent_verifier_observation(line, payload, part_receipt)
                               if kind == "focused_verifier" else None)
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
                review.add(parts, proof, index, record["event_sha256"], last_mutation_index)
        if evidence["complete"] and failures != evidence["failed_count"]:
            raise InspectionError("EVIDENCE_FAILURE_COUNT_MISMATCH")
        if not evidence["complete"] and not (failures <= evidence["failed_count"] <= total_commands):
            raise InspectionError("EVIDENCE_FAILURE_COUNT_MISMATCH")
        if evidence["failed_count"] != expected_evidence["failed_count"]:
            raise InspectionError("EVIDENCE_FAILURE_COUNT_MISMATCH")
        resolved = {}
        if (evidence["complete"] and file_blocker is None and capability_request is not None
                and verifier_failures and any(row["verification_evidence"] is not None
                                             for _, row in verifier_passes)):
            try:
                if bound_contract is None:
                    bound_contract = _inspection_contract(run, terminal, capability_request,
                        result["artifacts"], require_artifact=False)
                sources = _resolution_sources(capability_request, bound_contract, file_postimages)
                if sources is not None:
                    passes = [(i, row) for i, row in verifier_passes
                              if i > last_mutation_index and verifier_calls[row["call_id"]] == 1
                              and _verification_matches(row, bound_contract, sources)]
                    for failed_index, rows in verifier_failures.items():
                        links = []
                        for failed in rows:
                            match = next(((i, row) for i, row in passes
                                          if i > failed_index
                                          and row["verifier_index"] == failed["verifier_index"]
                                          and verifier_calls[failed["call_id"]] == 1), None)
                            if match is None:
                                break
                            passing_index, passed = match
                            links.append({"passing_event_index": passing_index,
                                          "verifier_index": passed["verifier_index"],
                                          "verifier_sha256": passed["verification_evidence"]["verifier_sha256"],
                                          "failed_call_id": failed["call_id"], "passing_call_id": passed["call_id"]})
                        if len(links) == len(rows):
                            resolved[failed_index] = {"status": "resolved", "verifiers": links}
            except (InspectionError, OSError, ValueError, TypeError, KeyError, IndexError,
                    RecursionError, OverflowError):
                pass  # Missing optional proof never excuses the original failure.
        non_attempts = {index: proof for index, proof in rejection_candidates.items()
                        if rejection_calls[proof["call_id"]] == 1}
        for command in commands:
            if command["event_index"] in resolved:
                command["failure_resolution"] = resolved[command["event_index"]]
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
        if known_failure and (not non_attempts or verifier_failures):
            result["verifier_failure_resolution"] = {
                "schema_version": "nokiy_focused_verifier_resolution_v1",
                "resolved_count": len(resolved), "unresolved_count": unresolved,
                "effective_command_success": effective_success,
            }
        if non_attempts:
            result["source_read_rejection_reconciliation"] = {
                "schema_version": "nokiy_source_read_rejection_reconciliation_v1",
                "not_attempted_count": len(non_attempts), "unresolved_count": unresolved,
                "effective_command_success": effective_success,
            }
        result["source_read_efficiency"] = reads.summary(
            evidence["complete"], result["command_success"] == "known_success")
        blocker = ("EVIDENCE_INDEX_PARTIAL" if not evidence["complete"] else
                   "COMMAND_FAILURE" if unresolved else
                    "EXECUTION_PROOF_UNAVAILABLE" if total_commands and effective_success != "known_success" else
                   "COMMAND_IDENTITY_OMITTED" if any(c["command"] is None for c in commands) else
                   file_blocker if file_blocker else
                    "EXECUTION_PROOF_UNAVAILABLE" if not total_commands and result["file_change_success"] != "known_success" else
                   "CLEANUP_UNPROVEN" if (not cleanup_bound or result["cleanup"]["historical_pass"] is not True
                       or any(result["cleanup"][k] != "absent"
                              for k in ("engine_pid", "supervisor_pid"))) else
                   "TERMINAL_NOT_AVAILABLE" if terminal.get("status") != "RESULT_AVAILABLE" or detail is not None else None)
        if blocker is None and delivery.marker is not None and delivery.marker["terminal_status"] == "blocked":
            blocker = "WORKER_TERMINAL_EVIDENCE_BLOCKED"
        result["first_blocker"] = blocker
        result["status"] = "EVIDENCE_VERIFIED" if blocker is None else "INCOMPLETE_EVIDENCE"
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
        if review is not None and result["status"] == "EVIDENCE_VERIFIED":
            context = review.project(terminal, verifier_calls)
            if context is not None:
                # Add last: recorded source bodies must never shorten an evidence page.
                result["parent_review_context"] = context
                if len(caller._canonical_bytes(result)) > MAX_PAGE_BYTES:
                    result.pop("parent_review_context")
    except caller.EmbeddedNokiyError as exc:
        result["first_blocker"] = exc.code
    except UnicodeError:
        result["first_blocker"] = "TRAJECTORY_INVALID_JSON"
    except InspectionError as exc:
        result["status"] = "INCOMPLETE_EVIDENCE"
        result["first_blocker"] = exc.code
    return result


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
                context = evidence.get("parent_review_context")
                if (context is not None and (not isinstance(context, dict)
                        or context.get("terminal_content_sha256") != caller._canonical_sha256(terminal))):
                    evidence.pop("parent_review_context", None)
                # The original terminal already carries identity, usage and artifact
                # hashes. Keep one copy and use event_index to locate full evidence.
                for key in ("artifacts", "usage", "expected_thread_id", "request_id",
                            "terminal_status", "terminal_first_blocker"):
                    evidence.pop(key, None)
                if evidence.get("counts", {}).get("file_changes") == 0:
                    evidence.pop("file_changes", None)
                    evidence.pop("file_change_success", None)
                evidence["commands"] = [
                    {key: row[key] for key in ("event_index", "command", "exit_code",
                                              "receipt_check", "execution_proof")}
                    | ({"evidence_source": row["evidence_source"], "command_id": row["command_id"]}
                       if "evidence_source" in row else {})
                    | ({"failure_resolution": row["failure_resolution"]} if "failure_resolution" in row else {})
                    | ({"pre_execution_rejection": row["pre_execution_rejection"]}
                       if "pre_execution_rejection" in row else {})
                    | ({"diagnostic_excerpt": row["verifier_output_excerpt"]}
                       if "verifier_output_excerpt" in row else
                       {"diagnostic_excerpt": row["diagnostic_excerpt"].encode("utf-8", errors="replace")[
                           :MAX_DIAGNOSTIC_BYTES].decode("utf-8", errors="ignore")}
                       if (row["execution_proof"] == "known_failure"
                           and isinstance(row.get("diagnostic_excerpt"), str)
                           and row["diagnostic_excerpt"]) else {})
                    for row in evidence["commands"]
                ]
            except (OSError, ValueError, TypeError, KeyError, IndexError,
                    UnicodeError, RecursionError, OverflowError):
                evidence["first_blocker"] = "INSPECTION_UNAVAILABLE"
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
    if len(caller._canonical_bytes(projected)) > projection_budget:
        # Discard source bodies before identities, commands, gaps or recovery evidence.
        evidence.pop("parent_review_context", None)
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
                                            "source_read_rejection_reconciliation")
               if key in evidence},
        }
    if "capability_gap" in projected and len(caller._canonical_bytes(projected)) > projection_budget:
        projected.pop("capability_gap")
    return projected


def summarize_terminal(projected: dict[str, Any], artifact_root: Path,
                       request_id: str) -> dict[str, Any]:
    """Select return fields from an already projected terminal; do not inspect again."""
    fields = (
        "status", "first_typed_blocker", "first_blocker", "request_id", "request_sha256",
        "native_thread_id", "execution_id", "session_id", "model", "model_route",
        "requested_model", "observed_model", "model_provider",
        "reasoning_effort", "requested_reasoning_effort", "observed_reasoning_effort",
        "service_tier", "requested_service_tier", "observed_service_tier",
        "requested_model_provider", "observed_model_provider", "result_text",
        "requested_terminal_delivery", "observed_terminal_delivery", "terminal_evidence",
        "result_truncated", "result_artifact", "usage", "wall_time_seconds",
        "runtime_exit_code", "cleanup_pass", "continuation_owner", "mission_acceptance",
        "authority_effect", "fallback_used", "result_inspection", "capability_gap",
        "inspection_summary", "inspection_summary_diagnostic", "inspection_summary_omission",
    )
    inspection = projected["result_inspection"]
    return {
        "schema_version": "nokiy_terminal_summary_v1",
        "terminal_schema_version": projected.get("schema_version"),
        **{key: projected[key] for key in fields if key in projected},
        "terminal_path": str(artifact_root / request_id / "terminal.json"),
        # Only inspection can establish this hash. Never hash the projected response.
        "terminal_sha256": inspection.get("terminal_sha256"),
    }


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
            or cleanup.get("historical_pass") is not True
            or any(cleanup.get(key) != "absent" for key in ("engine_pid", "supervisor_pid"))):
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--expected-thread-id", required=True)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--command-stream", choices=("history", "priority"), default="history",
                        help="Offset stream; priority omits only proven successful typed source-read-only commands")
    parser.add_argument("--command-sha256", action="append", default=[],
                        help="Exact UTF-8 command SHA-256 to display; all evidence is still verified")
    args = parser.parse_args()
    result = inspect(args.artifact_root, args.request_id, args.expected_thread_id,
                     offset=args.offset, command_sha256=tuple(args.command_sha256),
                     command_stream=args.command_stream)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "EVIDENCE_VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
