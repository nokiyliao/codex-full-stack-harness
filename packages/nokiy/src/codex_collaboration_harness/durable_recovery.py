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

MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 4096
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_CONTEXT_BYTES = 8 * 1024 * 1024
MAX_FEED_ROWS = 4096
MAX_FEED_BYTES = 8 * 1024 * 1024
_COMMAND_FIELDS = ("command_id", "command_index", "command_run_id", "command_type",
                   "provider_tool_call_id", "step")


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
            # The projection wrapper is metadata-bound; inner IDs stay canonical.
            if (event.get("tool_name") != "command_run"
                    or len(suffix) != 16 or any(char not in "0123456789abcdef" for char in suffix)
                    or not isinstance(metadata, dict)
                    or metadata.get("kind") != "mano_tool_call"
                    or metadata.get("tool") != "command_run"
                    or metadata.get("runtime_id") != runtime
                    or metadata.get("session_id") != session
                    or state.get("status") != "completed"
                    or not isinstance(state.get("output"), str)):
                raise reader.InspectionError("DURABLE_FEED_IDENTITY_MISMATCH")
        commands = inputs.get("commands") if isinstance(inputs, dict) else None
        if not isinstance(commands, list) or not commands or len(commands) > 128:
            raise reader.InspectionError("DURABLE_FEED_COMMANDS_INVALID")
        declared, command_hashes = {}, {}
        event_sha = caller._canonical_sha256(event)
        for index, command in enumerate(commands):
            provider = command.get("provider_tool_call_id") if isinstance(command, dict) else None
            if (not isinstance(provider, str) or not provider.startswith("call_")
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
            for key in _COMMAND_FIELDS:
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
                if status not in ("ready", "running", "error", "completed"):
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
                if (status == "completed" or "id" in value
                        or not isinstance(value.get("command"), (str, dict))):
                    raise reader.InspectionError("DURABLE_COMMAND_STATE_UNSUPPORTED")
                continue
            if type(value["success"]) is not bool or status not in (None, "completed"):
                raise reader.InspectionError("DURABLE_COMMAND_STATE_UNSUPPORTED")
            if not isinstance(value.get("command"), dict) or value.get("id") != identifier:
                raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
            yield cursor, {**declared[identifier], "status": "completed", "result": value}, "durable_session_feed", event_sha


def recover(run: Path, terminal: dict, events: list[dict]) -> dict:
    # Reuse the inspection boundary, including strict JSON and receipt reads.
    from . import result_inspection as reader

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
    paths = {p for _, p in locations}
    if len(paths) != 1:
        raise reader.InspectionError("DURABLE_DATABASE_PATH_INVALID")
    path = Path(next(iter(paths)))
    try:
        relative = path.relative_to(run).as_posix()
    except ValueError as exc:
        raise reader.InspectionError("DURABLE_DATABASE_PATH_INVALID") from exc
    parts = Path(relative).parts
    if (len(parts) != 6 or parts[:3] != ("execution-state", "session_log", "workspaces")
            or reader._SHA.fullmatch(parts[3]) is None
            or parts[4:] != (".tura", "session_log.sqlite3")):
        raise reader.InspectionError("DURABLE_DATABASE_PATH_INVALID")
    records = []
    total_bytes = 0
    runtime_ids = {r for r, _ in locations}
    with _database(run, relative, reader) as (db, ref):
        runtimes = db.execute(
            "SELECT runtime_id, lease_active, terminal FROM runtimes WHERE session_id=? LIMIT ?",
            (session, MAX_RECORDS + 1)).fetchall()
        if (len(runtimes) != len(locations)
                or set(r for r, _, _ in runtimes) != set(r for r, _ in locations)
                or any(lease != 0 or end != 1 for _, lease, end in runtimes)):
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
    reads = reader._ReadEfficiency()
    usage, models = {}, set()
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
            continue
        identifier = record.get("command_id")
        if (record.get("session_id") != session or not isinstance(identifier, str)
                or not identifier.startswith(runtime + ".tool.command_run:call_")):
            raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
        if record.get("status") == "ready":
            command = record.get("command")
            if not isinstance(command, dict):
                raise reader.InspectionError("DURABLE_COMMAND_DUPLICATE")
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
        matched = reader._receipt(run, output)
        code = receipt["exit_code"]
        if ("exit_code" in output and (type(output["exit_code"]) is not int or output["exit_code"] != code)):
            raise reader.InspectionError("EXIT_CODE_CONFLICT")
        line = command.get("command_line")
        if not isinstance(line, str):
            raise reader.InspectionError("DURABLE_COMMAND_IDENTITY_MISMATCH")
        line_bytes = line.encode()
        row = {"event_index": sequence, "event_sha256": event_sha or caller._canonical_sha256(record),
               "evidence_source": source, "command_id": identifier,
               "command_sha256": hashlib.sha256(line_bytes).hexdigest(),
               "command": line if value.get("command_type") != "apply_patch" and len(line_bytes) <= reader.MAX_COMMAND_BYTES else None,
               "command_bytes": len(line_bytes), "status": record["status"], "exit_code": code,
               "receipt_check": matched, "execution_proof": (
                   "known_success" if code == 0 and value.get("success") is True else
                   "known_failure" if code != 0 else "unavailable")}
        commands.append(row)
        if len(commands) > 128:
            raise reader.InspectionError("DURABLE_COMMAND_LIMIT_EXCEEDED")
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
    complete = (feed_coverage["availability"] == "present"
                and feed_coverage["unobserved_runtime_count"] == 0 and set(ready) == set(done))
    return {"commands": commands, "artifacts": artifacts,
            "read_efficiency": reads.summary(complete, all(c["execution_proof"] == "known_success" for c in commands)),
            "details": {"schema_version": "nokiy_durable_recovery_v2", "replay_allowed": False,
                        "integrity": "current_snapshot_not_sealed_by_original_terminal",
                        "runtime_count": len(runtime_ids), "command_count": len(commands),
                        "pending_command_count": len(set(ready) - set(done)), "command_index_complete": complete,
                        "command_index_scope": "observed_request_local_commands",
                        "feed_coverage": feed_coverage, "unobserved_filesystem_effects": "UNKNOWN",
                        "file_changes": edits, "historical_postimages": list(postimages.values()),
                        "preimage_proof": "unavailable", "current_postimage_proof": "not_read",
                        "reported_usage": {"coverage": "partial", "runtime_count": len(usage),
                                           "totals": {k: sum(v[k] for v in usage.values() if k in v)
                                                      for k in reader.USAGE_FIELDS if any(k in v for v in usage.values())}},
                        "reported_models": sorted(models)}}
