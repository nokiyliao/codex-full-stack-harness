# SPDX-License-Identifier: MIT
"""Inspect quiescent, request-local tool results; never resume an execution.

These snapshots were not sealed into the old terminal. They supplement a failed
terminal, not replace its status, original artifacts or parent acceptance.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from itertools import chain
import os
from pathlib import Path
import sqlite3
import stat

from . import embedded_nokiy as caller
from . import file_change_evidence

MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_RECORDS = 16384
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_CONTEXT_BYTES = 64 * 1024 * 1024
MAX_FEED_ROWS = 16384
MAX_FEED_BYTES = 64 * 1024 * 1024
_COMMAND_FIELDS = ("command_id", "command_index", "command_run_id", "command_type",
                   "provider_tool_call_id", "step")


def _valid_provider_tool_call_id(value):
    """Bound opaque metadata only; exact structured equality supplies identity."""
    if not isinstance(value, str) or not 0 < len(value) <= 256:
        return False
    try:
        return (len(value.encode("utf-8")) <= 256
                and not any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in "\u2028\u2029" for c in value))
    except UnicodeError:
        return False


def _policy_denial(value, command, reader):
    """Recognize the router's bound pre-execution rejection, never a receipt."""
    if not isinstance(value, dict) or value.get("command_type") != "jspace":
        return False
    fixed = {"success": False, "effect_state": "not_started", "mutation_count": 0,
             "authority_effect": "none", "delivery_state": "deterministic_policy_denial",
             "replayable": True}
    code = value.get("jspace_error_code")
    if (not isinstance(command, dict)
            or any(type(value.get(k)) is not type(v) or value.get(k) != v for k, v in fixed.items())
            or not isinstance(value.get("command"), dict)
            or caller._canonical_sha256(value["command"]) != caller._canonical_sha256(command)
            or any(type(value.get(k)) is not type(command.get(k)) or value.get(k) != command.get(k)
                   for k in _COMMAND_FIELDS if k != "command_type")
            or ("id" in value and value["id"] != command.get("command_id"))
            or not isinstance(code, str) or not code.startswith("JSPACE_")
            or not isinstance(value.get("error"), str) or not value["error"].startswith(code + ":")
            or any(not isinstance(value.get(k), str) for k in ("operation", "target"))
            or any(k in value for k in ("output", "terminal_receipt", "terminal_receipt_path"))):
        raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
    return True


def _failed_usage_summary(observations):
    fields = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")
    totals, reported, unknown, invalid = {}, 0, 0, 0
    for attempts in observations.values():
        for attempt in attempts:
            value = attempt.get("usage") if isinstance(attempt, dict) else None
            if value is None:
                unknown += 1
                continue
            if (not isinstance(value, dict) or value.get("invalid_fields") != []
                    or any(v is not None and (type(v) is not int or not 0 <= v < 10**18)
                           for k in fields for v in [value.get(k)])):
                invalid += 1
                continue
            counts = {k: value[k] for k in fields if value.get(k) is not None}
            if (all(k in counts for k in fields[:3])
                    and counts["input_tokens"] + counts["output_tokens"] != counts["total_tokens"]):
                invalid += 1
                continue
            if any(counts[a] > counts[b] for a, b in (
                    ("cached_input_tokens", "input_tokens"), ("reasoning_tokens", "output_tokens"))
                   if a in counts and b in counts):
                invalid += 1
                continue
            if not counts:
                unknown += 1
                continue
            reported += 1
            for key, count in counts.items():
                totals[key] = totals.get(key, 0) + count
    return {"coverage": "partial", "scope": "reported_failed_attempts_only",
            "runtime_count": len(observations), "attempt_count": sum(map(len, observations.values())),
            "reported_attempt_count": reported, "unknown_attempt_count": unknown,
            "invalid_attempt_count": invalid, "totals": totals,
            "billing": "UNKNOWN", "included_in_reported_usage_totals": False}


@contextmanager
def _database(run, relative, reader):
    """Pin the opened inode through /dev/fd; SQLite cannot create sidecars."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in (".", "..") for p in parts):
        raise reader.InspectionError("DURABLE_DATABASE_PATH_INVALID")
    directory = os.open(run, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    source = None
    connection = None
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        for suffix in ("-wal", "-journal"):
            try:
                sidecar = os.stat(parts[-1] + suffix, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(sidecar.st_mode) or sidecar.st_size:
                raise reader.InspectionError("DURABLE_DATABASE_UNSETTLED")
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(source)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_DATABASE_BYTES:
            raise reader.InspectionError("DURABLE_DATABASE_UNSAFE")
        digest = hashlib.sha256()
        for start in range(0, before.st_size, 1024 * 1024):
            digest.update(os.pread(source, min(1024 * 1024, before.st_size - start), start))
        connection = sqlite3.connect(f"file:/dev/fd/{source}?mode=ro&immutable=1", uri=True)
        connection.execute("PRAGMA query_only=ON")
        calls = 0

        def budget():
            nonlocal calls
            calls += 1
            return calls > 1000

        connection.set_progress_handler(budget, 1000)
        yield connection, {"path": relative, "sha256": digest.hexdigest(), "bytes": before.st_size}
        current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        def identity(s):
            return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
        if identity(before) != identity(os.fstat(source)) or identity(before) != identity(current):
            raise reader.InspectionError("DURABLE_DATABASE_CHANGED")
        for suffix in ("-wal", "-journal"):
            try:
                sidecar = os.stat(parts[-1] + suffix, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(sidecar.st_mode) or sidecar.st_size:
                raise reader.InspectionError("DURABLE_DATABASE_UNSETTLED")
    except (OSError, sqlite3.Error) as exc:
        raise reader.InspectionError("DURABLE_DATABASE_UNAVAILABLE") from exc
    finally:
        if connection is not None:
            connection.close()
        if source is not None:
            os.close(source)
        os.close(directory)


def _runtime_database_relative(run, paths, reader):
    if len(paths) != 1 or any(not isinstance(p, str) for p in paths):
        raise reader.InspectionError("DURABLE_DATABASE_PATH_INVALID")
    try:
        relative = Path(next(iter(paths))).relative_to(run).as_posix()
    except ValueError as exc:
        raise reader.InspectionError("DURABLE_DATABASE_PATH_INVALID") from exc
    parts = Path(relative).parts
    if (len(parts) != 6 or parts[:3] != ("execution-state", "session_log", "workspaces")
            or reader._SHA.fullmatch(parts[3]) is None
            or parts[4:] != (".tura", "session_log.sqlite3")):
        raise reader.InspectionError("DURABLE_DATABASE_PATH_INVALID")
    return relative


def provider_stream_failure_evidence(run, prefix, reader):
    """Read bounded tails from the exact failed prefix, never the whole session."""
    session, end = prefix["session_id"], prefix["next_sequence"]
    integer = lambda n: type(n) is int and 0 <= n <= 2**63 - 1
    if not integer(end) or end == 0:
        return None
    with _database(run, "execution-state/session_log/index.sqlite3", reader) as (index, index_ref):
        paths = index.execute("SELECT DISTINCT workspace_db_path FROM runtime_locations WHERE session_id=? LIMIT 2",
                              (session,)).fetchall()
        relative = _runtime_database_relative(run, {row[0] for row in paths}, reader)
        with _database(run, relative, reader) as (db, database_ref):
            if db.execute("SELECT next_context_sequence,retained_from_sequence FROM sessions WHERE session_id=?",
                          (session,)).fetchall() != [(end, 0)]:
                return None
            tail = db.execute("SELECT sequence,record_json FROM session_context_records WHERE session_id=? "
                              "AND sequence>=? AND sequence<? ORDER BY sequence DESC LIMIT 16",
                              (session, max(0, end - 16), end)).fetchall()
            if [row[0] for row in tail] != list(range(end - 1, max(-1, end - 17), -1)):
                return None
            runtime = None
            for _, raw in tail:
                if not isinstance(raw, str) or len(raw.encode()) > MAX_RECORD_BYTES:
                    return None
                record = reader._json(raw.encode())
                if record.get("type") in ("runtime_usage", "runtime_provider_observation"):
                    runtime = record.get("runtime_id")
                    break
            if not isinstance(runtime, str):
                return None
            if index.execute("SELECT workspace_db_path FROM runtime_locations WHERE session_id=? AND runtime_id=? LIMIT 2",
                             (session, runtime)).fetchall() != [(str(run / relative),)]:
                return None
            rows = db.execute("SELECT session_id,lease_active,terminal,last_event_seq FROM runtimes "
                              "WHERE runtime_id=? LIMIT 2", (runtime,)).fetchall()
            if len(rows) != 1 or rows[0][:3] != (session, 0, 1) or not integer(rows[0][3]):
                return None
            last = rows[0][3]
            events = db.execute("SELECT event_seq,event_json FROM runtime_events WHERE runtime_id=? "
                                "AND event_seq>=? AND event_seq<=? ORDER BY event_seq DESC LIMIT 16",
                                (runtime, max(1, last - 15), last)).fetchall()
            if [row[0] for row in events] != list(range(last, max(0, last - 16), -1)):
                return None
            outputs, failure = [], None
            for sequence, raw in events:
                if not isinstance(raw, str) or len(raw.encode()) > MAX_RECORD_BYTES:
                    return None
                event = reader._json(raw.encode())
                if event.get("runtime_id", runtime) != runtime or event.get("session_id", session) != session:
                    return None
                if "error" in event and failure is None:
                    failure = (sequence, event["error"])
                if "output" in event:
                    outputs.append((sequence, event["output"]))
            snapshots = [(seq, output["provider_stream_failure"]) for seq, output in outputs
                         if isinstance(output, dict) and "provider_stream_failure" in output]
            if len(snapshots) != 1 or not outputs or outputs[0][0] != snapshots[0][0] or failure is None:
                return None
            sequence, snapshot = snapshots[0]
            failed_seq, error = failure
            if (type(snapshot) is not dict or snapshot.get("runtime_id") != runtime or sequence >= failed_seq
                    or type(error) is not dict or type(error.get("retry_allowed")) is not bool
                    or type(error.get("fallback_allowed")) is not bool):
                return None
            after_effect = snapshot.get("command_effect_started")
            if (type(after_effect) is not bool
                    or error.get("error_code") != ("CALL_FAILED_AFTER_COMMAND_EFFECT" if after_effect else "CALL_FAILED")
                    or after_effect and (error["retry_allowed"] or error["fallback_allowed"])):
                return None
            evidence = {"snapshot": snapshot, "session_id": session, "output_event_seq": sequence,
                        "failure_event_seq": failed_seq, "last_event_seq": last,
                        "index_artifact": index_ref, "database_artifact": database_ref}
    return evidence


def _feed(db, session, runtime_ids, reader):
    """Read only the original session's already-checked runtime snapshots."""
    available = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_feed_events'").fetchone() is not None
    coverage = {"availability": "present" if available else "unavailable_legacy_schema",
                "event_count": 0, "runtime_count": 0, "unobserved_runtime_count": len(runtime_ids)}
    if not available:
        return [], coverage
    records, seen, observed = [], set(), set()
    total_bytes, previous = 0, -1
    # The runtimes join binds to the exact set checked by recover(), without an
    # unbounded IN parameter list or admitting a foreign runtime's feed rows.
    for cursor, runtime, event_id, raw in db.execute(
            "SELECT f.cursor, f.runtime_id, f.event_id, f.event_json FROM session_feed_events AS f "
            "JOIN runtimes AS r ON r.session_id=f.session_id AND r.runtime_id=f.runtime_id "
            "WHERE f.session_id=? AND r.session_id=? ORDER BY f.cursor LIMIT ?",
            (session, session, MAX_FEED_ROWS + 1)):
        if len(records) >= MAX_FEED_ROWS or not isinstance(raw, str):
            raise reader.InspectionError("DURABLE_FEED_LIMIT_EXCEEDED")
        encoded = raw.encode("utf-8")
        total_bytes += len(encoded)
        if len(encoded) > MAX_RECORD_BYTES or total_bytes > MAX_FEED_BYTES:
            raise reader.InspectionError("DURABLE_FEED_LIMIT_EXCEEDED")
        if (type(cursor) is not int or cursor <= previous or runtime not in runtime_ids
                or not isinstance(event_id, str) or not event_id or event_id in seen):
            raise reader.InspectionError("DURABLE_FEED_IDENTITY_MISMATCH")
        previous = cursor
        seen.add(event_id)
        event = reader._json(encoded)
        if any(key in event and (type(event[key]) is not type(value) or event[key] != value)
               for key, value in (("session_id", session), ("runtime_id", runtime),
                                  ("event_id", event_id), ("cursor", cursor))):
            raise reader.InspectionError("DURABLE_FEED_IDENTITY_MISMATCH")
        records.append((cursor, runtime, event))
        if event.get("event") == "tool_call_updated":
            observed.add(runtime)
    coverage.update(event_count=len(records), runtime_count=len(observed),
                    unobserved_runtime_count=len(runtime_ids - observed))
    return records, coverage


def _feed_commands(records, session, reader):
    """Project full per-command results, never an aggregate success/error bit.

    Progress summaries cannot establish completion, even in error snapshots.
    Full results still go through the same receipt, scope and dedup checks as context.
    Verified provider-round boundaries are not command or effect evidence.
    """
    for cursor, runtime, event in records:
        if event.get("event") != "tool_call_updated":
            continue
        call = runtime + ".tool.command_run"
        state = event.get("state")
        if (not isinstance(state, dict)
                or any(key in event and event[key] != expected
                       for key, expected in (("runtime_id", runtime), ("session_id", session)))):
            raise reader.InspectionError("DURABLE_FEED_IDENTITY_MISMATCH")
        inputs = state.get("input")
        metadata = event.get("metadata")
        if event.get("tool_name") == "runtime":
            runtime_status = event.get("runtime_status")
            if (event.get("call_id") != runtime
                    or not isinstance(metadata, dict)
                    or metadata.get("runtime_id") != runtime
                    or metadata.get("session_id") != session
                    or not isinstance(runtime_status, dict)
                    or runtime_status.get("runtime_id") != runtime
                    or ("session_id" in runtime_status and runtime_status["session_id"] != session)
                    or state.get("status") != "completed"
                    or not isinstance(inputs, dict)
                    or not all(key in inputs for key in ("messages", "options", "tools"))
                    or "commands" in inputs):
                raise reader.InspectionError("DURABLE_FEED_IDENTITY_MISMATCH")
            continue
        if event.get("call_id") != call:
            outer = event.get("call_id")
            prefix = runtime + "-"
            suffix = outer[len(prefix):] if isinstance(outer, str) and outer.startswith(prefix) else ""
            # A failed batch's wrapper ends as a bare error notice. With no output
            # and no updates it carries no results, so it neither proves nor erases.
            bare_error = (state.get("status") == "error" and state.get("output") is None
                          and not event.get("command_updates"))
            # The projection wrapper is metadata-bound; inner IDs stay canonical.
            if (event.get("tool_name") != "command_run"
                    or len(suffix) != 16 or any(char not in "0123456789abcdef" for char in suffix)
                    or not isinstance(metadata, dict)
                    or metadata.get("kind") != "mano_tool_call"
                    or metadata.get("tool") != "command_run"
                    or metadata.get("runtime_id") != runtime
                    or metadata.get("session_id") != session
                    or not (bare_error or (state.get("status") == "completed"
                                           and isinstance(state.get("output"), str)))):
                raise reader.InspectionError("DURABLE_FEED_IDENTITY_MISMATCH")
        commands = inputs.get("commands") if isinstance(inputs, dict) else None
        if not isinstance(commands, list) or not commands or len(commands) > 128:
            raise reader.InspectionError("DURABLE_FEED_COMMANDS_INVALID")
        declared, command_hashes = {}, {}
        event_sha = caller._canonical_sha256(event)
        for index, command in enumerate(commands):
            provider = command.get("provider_tool_call_id") if isinstance(command, dict) else None
            if (not _valid_provider_tool_call_id(provider)
                    or type(command.get("command_index")) is not int or command["command_index"] != index
                    or command.get("command_run_id") != call
                    or command.get("command_id") != f"{call}:{provider}:{index}"
                    or not isinstance(command.get("command_type"), str) or not command["command_type"]
                    or not isinstance(command.get("command_line"), str)
                    or type(command.get("step")) is not int or command["step"] < 1):
                raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
            common = {key: command[key] for key in _COMMAND_FIELDS}
            common.update(type="streamed_command_event", session_id=session, runtime_id=runtime)
            identifier = command["command_id"]
            declared[identifier] = {**common, "command": command}
            command_hashes[identifier] = caller._canonical_sha256(command)
            yield cursor, {**declared[identifier], "status": "ready"}, "durable_session_feed", event_sha

        def identify(value):
            identifier = value.get("command_id") if isinstance(value, dict) else None
            if not isinstance(identifier, str) or identifier not in declared:
                raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
            denial = _policy_denial(value, declared[identifier]["command"], reader)
            for key in _COMMAND_FIELDS:
                if denial and key == "command_type":
                    continue
                # Wrappers and progress summaries need the four stable identity
                # fields, but need not repeat the declared command's metadata.
                if key in ("command_type", "step") and key not in value:
                    continue
                expected = declared[identifier][key]
                if type(value.get(key)) is not type(expected) or value.get(key) != expected:
                    raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
            command = declared[identifier]["command"]
            for key, expected in (("session_id", session), ("runtime_id", runtime),
                                  ("id", identifier), ("command_line", command["command_line"])):
                if key in value and (type(value[key]) is not type(expected) or value[key] != expected):
                    raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
            if "command" in value:
                actual = value["command"]
                if isinstance(actual, dict):
                    matches = caller._canonical_sha256(actual) == command_hashes[identifier]
                else:
                    matches = isinstance(actual, str) and actual == command.get("command", command["command_type"])
                if not matches:
                    raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
            return identifier

        results = []
        output = state.get("output")
        if isinstance(output, str):
            output = reader._json(output.encode("utf-8"))
            if not isinstance(output, dict) or not isinstance(output.get("command_events"), dict):
                raise reader.InspectionError("DURABLE_COMMAND_OUTPUT_INVALID")
            if (not isinstance(output.get("commands"), list)
                    or caller._canonical_sha256(output["commands"]) != caller._canonical_sha256(commands)):
                raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
            # Aggregate command_events and task_attribution are not completion proof.
            results = output.get("results")
        elif output is not None:
            if not isinstance(output, dict):
                raise reader.InspectionError("DURABLE_COMMAND_OUTPUT_INVALID")
            streamed = output.get("streamed_command_run_result")
            if streamed is not None:
                results = streamed.get("results") if isinstance(streamed, dict) else None
        if not isinstance(results, list) or len(results) > len(commands):
            raise reader.InspectionError("DURABLE_FEED_RESULTS_INVALID")
        updates = event.get("command_updates", [])
        if not isinstance(updates, list) or len(updates) > 2 * len(commands):
            raise reader.InspectionError("DURABLE_FEED_RESULTS_INVALID")

        def candidates():
            for value in results:
                yield value, None, None
            for update in updates:
                identifier = identify(update)
                if not isinstance(update.get("command"), dict):
                    raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
                status = update.get("status")
                # Tura publishes a receipted nonzero command as "failed"; the
                # context table stores that same result as completed/success=false.
                if status not in ("ready", "running", "error", "completed", "failed"):
                    raise reader.InspectionError("DURABLE_COMMAND_STATE_UNSUPPORTED")
                value = update.get("result")
                if value is None:
                    # Sparse notifications neither prove nor erase completion.
                    continue
                yield value, identifier, status

        for value, update_id, status in candidates():
            identifier = identify(value)
            if update_id is not None and identifier != update_id:
                raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
            if "success" not in value:
                raise reader.InspectionError("DURABLE_COMMAND_STATE_UNSUPPORTED")
            if value["success"] is None:
                if (status in ("completed", "failed") or "id" in value
                        or not isinstance(value.get("command"), (str, dict))):
                    raise reader.InspectionError("DURABLE_COMMAND_STATE_UNSUPPORTED")
                continue
            if (type(value["success"]) is not bool or status not in (None, "completed", "failed")
                    or (status == "failed" and value["success"] is not False)):
                raise reader.InspectionError("DURABLE_COMMAND_STATE_UNSUPPORTED")
            if (not _policy_denial(value, declared[identifier]["command"], reader)
                    and (not isinstance(value.get("command"), dict) or value.get("id") != identifier)):
                raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
            yield cursor, {**declared[identifier], "status": "completed", "result": value}, "durable_session_feed", event_sha


def _interrupted_scope(run: Path, reader):
    raw = reader._file(run / "supervision.json", reader.MAX_JSON)
    value = reader._json(raw)
    scope = value.get("scope")
    if (value.get("failure") != "GRAPH_SUPERVISOR_CALLER_EXITED"
            or not isinstance(scope, dict)
            or scope.get("engine_reaped") is not True
            or scope.get("no_live_descendants") is not True
            or scope.get("cleanup_error") is not None
            or type(scope.get("engine_exit_code")) is not int
            or scope["engine_exit_code"] >= 0
            or any(reader._process(scope.get(key)) != "absent"
                   for key in ("supervisor_pid", "engine_pid"))):
        raise reader.InspectionError("INTERRUPTED_CLEANUP_UNVERIFIED")
    return raw, scope


def inspect_interrupted(artifact_root: Path, request_id: str, expected_thread_id: str) -> dict:
    """Read a stopped, unsealed run without creating a terminal or replaying work."""
    from . import result_inspection as reader

    root = caller._plain_path(artifact_root, name="artifact_root", directory=True)
    if (not caller.REQUEST_ID_PATTERN.fullmatch(request_id)
            or not isinstance(expected_thread_id, str)
            or not caller.THREAD_ID_PATTERN.fullmatch(expected_thread_id)):
        raise reader.InspectionError("INTERRUPTED_IDENTITY_REQUIRED")
    run = root / request_id
    reader._directory(run)
    if os.path.lexists(run / "terminal.json"):
        raise reader.InspectionError("ORIGINAL_TERMINAL_PRESENT")
    original_raw = reader._file(run / "request-identity.json", reader.MAX_JSON)
    original = reader._json(original_raw)
    payload = {k: v for k, v in original.items() if k not in ("request_id", "request_sha256")}
    digest = caller._canonical_sha256(payload)
    if (original.get("native_thread_id") != expected_thread_id
            or original.get("request_id") != request_id
            or original.get("request_sha256") != digest
            or request_id != "tura_embedded_" + digest):
        raise reader.InspectionError("INTERRUPTED_REQUEST_IDENTITY_MISMATCH")
    supervision_raw, scope = _interrupted_scope(run, reader)
    core_raw = reader._file(run / "core.jsonl", MAX_CONTEXT_BYTES)
    events = [reader._json(line) for line in core_raw.splitlines() if line.strip()]
    # This identity carrier is not a terminal and is never persisted as one.
    recovered = recover(run, {"request_id": request_id, "request_sha256": digest,
                             "native_thread_id": expected_thread_id}, events, interrupted=True)
    for name, raw in (("request-identity.json", original_raw), ("core.jsonl", core_raw),
                      ("supervision.json", supervision_raw)):
        if reader._file(run / name, max(reader.MAX_JSON, MAX_CONTEXT_BYTES)) != raw:
            raise reader.InspectionError("INTERRUPTED_EVIDENCE_CHANGED")
    if os.path.lexists(run / "terminal.json") or _interrupted_scope(run, reader)[0] != supervision_raw:
        raise reader.InspectionError("INTERRUPTED_EVIDENCE_CHANGED")
    references = {name: {"path": str(run / name), "sha256": hashlib.sha256(raw).hexdigest(),
                         "bytes": len(raw)} for name, raw in (
        ("request-identity.json", original_raw), ("core.jsonl", core_raw), ("supervision.json", supervision_raw))}
    diagnostic = recovered["first_blocker_detail"]
    return {"schema_version": "nokiy_interrupted_recovery_v1",
            "status": "EXECUTION_UNCERTAIN" if diagnostic else "INTERRUPTED_EVIDENCE_AVAILABLE",
            "first_typed_blocker": "RECEIPT_PROOF_INVALID" if diagnostic else "NOKIY_CALLER_INTERRUPTED_BEFORE_TERMINAL",
            **({"first_blocker_detail": diagnostic} if diagnostic else {}),
            "observation_availability": recovered["details"]["observation_availability"],
            "aggregate_execution_proof": "unproven", "mission_acceptance": "parent_owned",
            "request_id": request_id, "native_thread_id": expected_thread_id,
            "terminal_present": False, "terminal_synthesized": False,
            "automatic_retry_allowed": False, "execution_replayed": False,
            "task_success": False, "cleanup_observation": scope,
            "commands": recovered["commands"], "durable_recovery": recovered["details"],
            "artifacts": {**recovered["artifacts"], **references},
            "next_action": "Parent reconciles effects and fresh source/ownership; admit only unfinished work. "
                           "Do not replay the consumed request or treat this snapshot as task success."}


def recover(run: Path, terminal: dict, events: list[dict], *, interrupted: bool = False) -> dict:
    # Reuse the inspection boundary, including strict JSON and receipt reads.
    from . import result_inspection as reader

    if interrupted:
        _interrupted_scope(run, reader)

    original = reader._json(reader._file(run / "request-identity.json", reader.MAX_JSON))
    payload = {k: v for k, v in original.items() if k not in ("request_id", "request_sha256")}
    digest = caller._canonical_sha256(payload)
    if (original.get("request_sha256") != digest or terminal.get("request_sha256") != digest
            or original.get("request_id") != "tura_embedded_" + digest
            or terminal.get("request_id") != original["request_id"]
            or terminal.get("native_thread_id") != original.get("native_thread_id")
            or not isinstance(original.get("model"), str)
            or not isinstance(original.get("workspace"), str)):
        raise reader.InspectionError("DURABLE_REQUEST_IDENTITY_MISMATCH")
    if terminal.get("original_jspace_artifact") is not None:
        contract_raw, contract_ref = reader._artifact(
            run, "jspace-original.json", terminal["original_jspace_artifact"], reader.MAX_JSON)
    else:
        # Failed V69 terminals omitted this ref too. The sealed request digest
        # still binds the original contract SHA; never read its external locator.
        contract_raw = reader._file(run / "jspace-original.json", reader.MAX_JSON)
        contract_ref = {"path": "jspace-original.json", "sha256": hashlib.sha256(contract_raw).hexdigest(),
                        "bytes": len(contract_raw)}
    reference = original.get("jspace_contract")
    contract = reader._json(contract_raw)
    if (not isinstance(reference, dict) or reference.get("sha256") != hashlib.sha256(contract_raw).hexdigest()
            or contract.get("repo_root") != original.get("workspace")
            or contract != reader._json(reader._file(run / "jspace.json", reader.MAX_JSON))):
        raise reader.InspectionError("DURABLE_CONTRACT_MISMATCH")
    workspace = Path(original["workspace"])
    session = "full-" + digest
    identity = {"session_id": session, "cwd": original.get("workspace"),
                "model": "codex/" + original["model"], "agent": original.get("execution_profile"),
                "reasoning_effort": original.get("reasoning_effort"),
                "service_tier": original.get("service_tier") or
                                ("priority" if original.get("model_acceleration") else "default")}
    starts = [e for e in events if e.get("type") == "turn.started"]
    bound = [e for e in events if e.get("type") in ("thread.started", "turn.started", "turn.completed")]
    if len(starts) != 1 or any(any(e.get(k) != v for k, v in identity.items()) for e in bound):
        raise reader.InspectionError("DURABLE_TRAJECTORY_IDENTITY_MISMATCH")
    artifacts = {"original_jspace": contract_ref}
    with _database(run, "execution-state/session_log/index.sqlite3", reader) as (db, ref):
        locations = db.execute(
            "SELECT runtime_id, workspace_db_path FROM runtime_locations WHERE session_id=? LIMIT ?",
            (session, MAX_RECORDS + 1)).fetchall()
        artifacts["durable_index"] = ref
    if not locations or len(locations) > MAX_RECORDS or len({r for r, _ in locations}) != len(locations):
        raise reader.InspectionError("DURABLE_RUNTIME_INDEX_INVALID")
    if any(not isinstance(p, str) for _, p in locations):
        raise reader.InspectionError("DURABLE_DATABASE_PATH_INVALID")
    relative = _runtime_database_relative(run, {p for _, p in locations}, reader)
    records = []
    total_bytes = 0
    runtime_ids = {r for r, _ in locations}
    with _database(run, relative, reader) as (db, ref):
        runtimes = db.execute(
            "SELECT runtime_id, lease_active, terminal FROM runtimes WHERE session_id=? LIMIT ?",
            (session, MAX_RECORDS + 1)).fetchall()
        if (len(runtimes) != len(locations)
                or set(r for r, _, _ in runtimes) != set(r for r, _ in locations)
                or any(lease not in (0, 1) or end not in (0, 1) for _, lease, end in runtimes)
                or not interrupted and any(lease != 0 or end != 1 for _, lease, end in runtimes)):
            raise reader.InspectionError("DURABLE_RUNTIME_NOT_TERMINAL")
        for sequence, raw in db.execute(
                "SELECT sequence, record_json FROM session_context_records WHERE session_id=? ORDER BY sequence LIMIT ?",
                (session, MAX_RECORDS + 1)):
            if len(records) >= MAX_RECORDS or not isinstance(raw, str):
                raise reader.InspectionError("DURABLE_CONTEXT_LIMIT_EXCEEDED")
            encoded = raw.encode("utf-8")
            total_bytes += len(encoded)
            if len(encoded) > MAX_RECORD_BYTES or total_bytes > MAX_CONTEXT_BYTES:
                raise reader.InspectionError("DURABLE_CONTEXT_LIMIT_EXCEEDED")
            records.append((sequence, reader._json(encoded)))
        artifacts["durable_context"] = ref
        feed_records, feed_coverage = _feed(db, session, runtime_ids, reader)
        if feed_coverage["availability"] == "present":
            artifacts["durable_feed"] = ref
    if [n for n, _ in records] != list(range(len(records))):
        raise reader.InspectionError("DURABLE_CONTEXT_SEQUENCE_INVALID")
    ready, done = {}, {}
    commands, edits, postimages = [], [], {}
    first_blocker_detail, invalid_receipt_count = None, 0
    reads = reader._ReadEfficiency()
    usage, models, failed_attempts = {}, set(), {}
    context = ((sequence, record, "durable_session_context", None) for sequence, record in records)
    for sequence, record, source, event_sha in chain(context, _feed_commands(feed_records, session, reader)):
        kind, runtime = record.get("type"), record.get("runtime_id")
        if kind not in ("streamed_command_event", "runtime_usage", "runtime_provider_observation"):
            continue
        if runtime not in runtime_ids:
            raise reader.InspectionError("DURABLE_RUNTIME_IDENTITY_MISMATCH")
        if kind == "runtime_usage":
            value = record.get("usage")
            if runtime in usage or not isinstance(value, dict):
                raise reader.InspectionError("DURABLE_USAGE_INVALID")
            usage[runtime] = {k: value[k] for k in reader.USAGE_FIELDS
                             if type(value.get(k)) is int and 0 <= value[k] < 10**18}
            continue
        if kind == "runtime_provider_observation":
            value = record.get("provider_observation")
            if isinstance(value, dict) and isinstance(value.get("model"), str):
                models.add(value["model"][:128])
            if isinstance(value, dict) and "failed_attempts" in value:
                attempts = value["failed_attempts"]
                if (not isinstance(attempts, list)
                        or runtime in failed_attempts and failed_attempts[runtime] != attempts):
                    raise reader.InspectionError("DURABLE_FAILED_USAGE_CONFLICT")
                failed_attempts[runtime] = attempts
            continue
        identifier = record.get("command_id")
        provider, index = record.get("provider_tool_call_id"), record.get("command_index")
        call = runtime + ".tool.command_run"
        if (record.get("session_id") != session or not isinstance(identifier, str)
                or record.get("command_run_id") != call or not _valid_provider_tool_call_id(provider)
                or type(index) is not int or not 0 <= index < 128 or identifier != f"{call}:{provider}:{index}"
                or not isinstance(record.get("command_type"), str) or not record["command_type"]
                or type(record.get("step")) is not int or record["step"] < 1):
            raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
        if record.get("status") == "ready":
            command = record.get("command")
            if not isinstance(command, dict):
                raise reader.InspectionError("DURABLE_COMMAND_DUPLICATE")
            if any(type(record.get(k)) is not type(command.get(k)) or record.get(k) != command.get(k)
                   for k in _COMMAND_FIELDS):
                raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
            command_sha = caller._canonical_sha256(command)
            if identifier in ready:
                if source != "durable_session_feed" or ready[identifier][1] != command_sha:
                    raise reader.InspectionError("DURABLE_COMMAND_DUPLICATE")
                continue
            ready[identifier] = command, command_sha
            if len(ready) > 128:
                raise reader.InspectionError("DURABLE_COMMAND_LIMIT_EXCEEDED")
            continue
        if record.get("status") != "completed":
            raise reader.InspectionError("DURABLE_COMMAND_STATE_UNSUPPORTED")
        value = record.get("result")
        entry = ready.get(identifier)
        command = entry[0] if entry is not None else None
        if _policy_denial(value, command, reader):
            if any(type(record.get(k)) is not type(command.get(k)) or record.get(k) != command.get(k)
                   for k in _COMMAND_FIELDS if k != "command_type") or record["command_type"] not in (command["command_type"], "jspace"):
                raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
            value_sha = caller._canonical_sha256(value)
            if identifier in done:
                if source != "durable_session_feed" or done[identifier] != value_sha:
                    raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
                continue
            done[identifier] = value_sha
            commands.append({"event_index": sequence,
                             "event_sha256": event_sha or caller._canonical_sha256(record),
                             "evidence_source": source, "command_id": identifier,
                             "command": command["command_line"], "status": "policy_denied",
                             "exit_code": None, "receipt_check": "not_applicable",
                             "execution_proof": "not_started", "jspace_error_code": value["jspace_error_code"],
                             "automatic_retry_allowed": False})
            if len(commands) > 128:
                raise reader.InspectionError("DURABLE_COMMAND_LIMIT_EXCEEDED")
            continue
        if (not isinstance(value, dict) or command is None or not isinstance(value.get("command"), dict)
                or caller._canonical_sha256(value["command"]) != entry[1] or value.get("id") != identifier
                or any(type(record.get(k)) is not type(command.get(k)) or record.get(k) != command.get(k)
                       or type(value.get(k)) is not type(command.get(k)) or value.get(k) != command.get(k)
                       for k in _COMMAND_FIELDS)):
            raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
        value_sha = caller._canonical_sha256(value)
        if identifier in done:
            if source != "durable_session_feed" or done[identifier] != value_sha:
                raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
            continue
        done[identifier] = value_sha
        if value.get("command_type") == "task_status":
            continue
        output = value.get("output")
        if not isinstance(output, dict):
            raise reader.InspectionError("DURABLE_COMMAND_OUTPUT_INVALID")
        receipt = output.get("terminal_receipt")
        if not isinstance(receipt, dict) or receipt.get("call_id") != identifier:
            raise reader.InspectionError("DURABLE_RECEIPT_IDENTITY_MISMATCH")
        diagnostic = None
        try:
            matched = reader._receipt(run, output, item=record, event_index=sequence)
        except reader.InspectionError as exc:
            # Only a bound individual receipt's settlement proof may be unknown.
            # Paths, copy mismatches and aggregate envelopes still reject the
            # entire snapshot, even after earlier commands have been inspected.
            if exc.code != "RECEIPT_PROOF_INVALID" or "results" in output or command["command_type"] == "command_run":
                raise
            diagnostic = exc.diagnostic
            if diagnostic is None:
                raise
            matched = "invalid"
            invalid_receipt_count += 1
            if first_blocker_detail is None:
                first_blocker_detail = diagnostic
        code = receipt["exit_code"] if diagnostic is None else None
        receipt_code = receipt.get("exit_code")
        if ("exit_code" in output and (type(output["exit_code"]) is not int
                or type(receipt_code) is int and output["exit_code"] != receipt_code)):
            raise reader.InspectionError("EXIT_CODE_CONFLICT")
        line = command.get("command_line")
        if not isinstance(line, str):
            raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
        line_bytes = line.encode()
        row = {"event_index": sequence, "event_sha256": event_sha or caller._canonical_sha256(record),
               "evidence_source": source, "command_id": identifier,
               "command_sha256": hashlib.sha256(line_bytes).hexdigest(),
               "command": line if value.get("command_type") != "apply_patch" and len(line_bytes) <= reader.MAX_COMMAND_BYTES else None,
               "command_bytes": len(line_bytes), "status": "unknown" if diagnostic else record["status"], "exit_code": code,
               "receipt_check": matched, "execution_proof": (
                   "unavailable" if diagnostic else
                   "known_success" if code == 0 and value.get("success") is True else
                   "known_failure" if code != 0 else "unavailable")}
        if diagnostic is not None:
            row["first_blocker_detail"] = diagnostic
        commands.append(row)
        if len(commands) > 128:
            raise reader.InspectionError("DURABLE_COMMAND_LIMIT_EXCEEDED")
        if diagnostic is not None:
            # Do not consume failed-proof payloads as reads, edits or postimages.
            # An unknown patch also fences previously observed postimages.
            if value.get("command_type") == "apply_patch":
                postimages.clear()
            continue
        reads.add({"command_type": value.get("command_type")}, output, matched)
        if value.get("command_type") == "apply_patch":
            changes = output.get("changes")
            if not isinstance(changes, list) or not changes:
                raise reader.InspectionError("DURABLE_FILE_CHANGE_INVALID")
            for change in changes:
                if not isinstance(change, dict):
                    raise reader.InspectionError("DURABLE_FILE_CHANGE_INVALID")
                path, kind = change.get("path"), change.get("kind")
                if (not isinstance(path, str) or "\0" in path
                        or change.get("move_path") is not None):
                    raise reader.InspectionError("DURABLE_FILE_CHANGE_SCOPE_INVALID")
                # Durable tool paths may be relative. Preserve their raw spelling
                # so the shared lexical validator can reject traversal/aliases;
                # never resolve or read the current workspace for this snapshot.
                target = path if Path(path).is_absolute() else f"{str(workspace).rstrip('/')}/{path}"
                try:
                    path, _ = file_change_evidence._target(workspace, contract, target, kind)
                except file_change_evidence.EffectError as exc:
                    raise reader.InspectionError("DURABLE_FILE_CHANGE_SCOPE_INVALID") from exc
                edits.append({"path": path, "kind": kind, "sequence": sequence,
                              "evidence_source": source, "command_id": identifier,
                              "execution_proof": row["execution_proof"]})
                if len(edits) > 128:
                    raise reader.InspectionError("DURABLE_FILE_CHANGE_LIMIT_EXCEEDED")
                postimages.pop(path, None)
        # Feed cursors and context sequences are different clocks. Do not infer
        # that a feed-only read happened after a context patch.
        elif (source == "durable_session_context" and value.get("command_type") == "source_read"
              and row["execution_proof"] == "known_success"):
            path, sha = output.get("path"), output.get("source_sha256")
            if (isinstance(path, str) and isinstance(sha, str) and reader._SHA.fullmatch(sha)
                    and any(e["path"] == path for e in edits)):
                postimages[path] = {"path": path, "sha256": sha, "sequence": sequence,
                                    "source": "historical_receipted_source_read"}
    complete = (not invalid_receipt_count and not interrupted and feed_coverage["availability"] == "present"
                and feed_coverage["unobserved_runtime_count"] == 0 and set(ready) == set(done))
    verified_count = sum(c["execution_proof"] in ("known_success", "known_failure") for c in commands)
    availability = ("partial" if invalid_receipt_count and verified_count else
                    "unavailable" if not verified_count else "available")
    return {"commands": commands, "artifacts": artifacts, "first_blocker_detail": first_blocker_detail,
            "read_efficiency": reads.summary(complete, all(c["execution_proof"] == "known_success" for c in commands)),
            "details": {"schema_version": "nokiy_durable_recovery_v2", "replay_allowed": False,
                        "integrity": "current_snapshot_not_sealed_by_original_terminal",
                        "runtime_count": len(runtime_ids), "command_count": len(commands),
                         "observation_availability": availability, "aggregate_execution_proof": "unproven",
                         "verified_command_count": verified_count, "invalid_receipt_count": invalid_receipt_count,
                        "interrupted_snapshot": interrupted,
                        "runtime_rows_terminal": all(lease == 0 and end == 1 for _, lease, end in runtimes),
                        "pending_command_count": len(set(ready) - set(done)), "command_index_complete": complete,
                        "command_index_scope": "observed_request_local_commands",
                        "feed_coverage": feed_coverage, "unobserved_filesystem_effects": "UNKNOWN",
                        "file_changes": edits, "historical_postimages": list(postimages.values()),
                        "preimage_proof": "unavailable", "current_postimage_proof": "not_read",
                        "reported_usage": {"coverage": "partial", "runtime_count": len(usage),
                                           "totals": {k: sum(v[k] for v in usage.values() if k in v)
                                                      for k in reader.USAGE_FIELDS if any(k in v for v in usage.values())}},
                        "reported_failed_attempt_usage": _failed_usage_summary(failed_attempts),
                        "reported_models": sorted(models)}}
