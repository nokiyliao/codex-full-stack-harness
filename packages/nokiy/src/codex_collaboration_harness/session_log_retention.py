"""Retire a sealed run's runtime session database after keeping per-call evidence.

Each Tura runtime keeps a private session DB under ``execution-state/session_log``
holding full request/response snapshots for every model call plus cumulative
tool-call stream updates (about 5-12 MB per worker run). Once a terminal is
sealed with command evidence, no caller path reads that DB: ``read_result``
uses command receipts and hash-bound terminal artifacts, while durable
recovery needs it only for BLOCKED terminals without command evidence or with
a provider stream-failure snapshot. ``runtime-calls.json`` keeps the small
per-call usage records and event timestamps that benchmarks reconcile against
the terminal's usage totals.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

SIDECAR_NAME = "runtime-calls.json"
SCHEMA_VERSION = "nokiy_runtime_calls_v1"
SESSION_LOG = ("execution-state", "session_log")
MAX_RUNTIMES = 4096
MAX_RECORD_BYTES = 64 * 1024
MAX_SIDECAR_BYTES = 4 * 1024 * 1024
_TIME_KEYS = ("called_at", "first_token_at", "finished_at", "created_at", "at", "timestamp")


def disposable(terminal: Any) -> bool:
    """True when no caller read path needs this terminal's runtime session DB."""
    if type(terminal) is not dict or terminal.get("cleanup_pass") is not True:
        return False
    if terminal.get("status") not in ("RESULT_AVAILABLE", "BLOCKED"):
        return False
    # Without command evidence, read_result rebuilds commands from the DB.
    if terminal.get("command_evidence_artifact") is None:
        return False
    digest = terminal.get("request_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        return False
    diagnostic = terminal.get("provider_failure_diagnostic")
    return not (isinstance(diagnostic, dict) and "stream_failure_snapshot" in diagnostic)


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def summarize(run: Path, session_id: str) -> dict[str, Any]:
    """Read only usage records and event timestamps; payload columns are dropped."""
    state = run.joinpath(*SESSION_LOG).resolve(strict=True)
    with closing(_connect(state / "index.sqlite3")) as index:
        rows = index.execute("SELECT workspace_db_path FROM sessions WHERE session_id=? LIMIT 2",
                             (session_id,)).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str):
        raise ValueError("session_workspace_not_unique")
    database = Path(rows[0][0]).resolve(strict=True)
    if not database.is_relative_to(state) or not database.is_file():
        raise ValueError("session_database_outside_session_log")
    with closing(_connect(database)) as db:
        records = [raw for raw, in db.execute(
            "SELECT record_json FROM session_context_records WHERE session_id=? "
            "AND json_extract(record_json, '$.type')='runtime_usage' ORDER BY sequence LIMIT ?",
            (session_id, MAX_RUNTIMES + 1))]
        runtimes = [runtime for runtime, in db.execute(
            "SELECT runtime_id FROM runtimes WHERE session_id=? ORDER BY rowid LIMIT ?",
            (session_id, MAX_RUNTIMES + 1))]
        if len(records) > MAX_RUNTIMES or len(runtimes) > MAX_RUNTIMES:
            raise ValueError("runtime_limit_exceeded")
        calls = []
        for runtime in runtimes:
            events, finished = [], None
            for sequence, raw in db.execute(
                    "SELECT event_seq, event_json FROM runtime_events WHERE runtime_id=? ORDER BY event_seq",
                    (runtime,)):
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError("runtime_event_invalid")
                name = event.get("event") if isinstance(event.get("event"), str) else None
                at = next((event[key] for key in _TIME_KEYS if isinstance(event.get(key), str)), None)
                events.append([sequence, name, at])
                if name == "runtime_finished" and isinstance(event.get("usage"), dict):
                    finished = event["usage"]
            calls.append({"runtime_id": runtime, "events": events, "finished_usage": finished})
    if any(not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RECORD_BYTES for raw in records):
        raise ValueError("usage_record_invalid")
    return {"schema_version": SCHEMA_VERSION, "session_id": session_id,
            "usage_records": records, "runtimes": calls}


def load(path: Path, session_id: str) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_SIDECAR_BYTES:
        raise ValueError("runtime_calls_too_large")
    value = json.loads(raw)
    if (type(value) is not dict or value.get("schema_version") != SCHEMA_VERSION
            or value.get("session_id") != session_id
            or type(value.get("usage_records")) is not list or type(value.get("runtimes")) is not list
            or any(not isinstance(raw, str) for raw in value["usage_records"])):
        raise ValueError("runtime_calls_invalid")
    return value


def retire(run: Path, terminal: Any) -> str:
    """Best effort after sealing: keep the DB whenever any step is unproven."""
    try:
        if not disposable(terminal):
            return "kept_required"
        target = run.joinpath(*SESSION_LOG)
        if target.is_symlink() or not target.is_dir():
            return "absent"
        session = "full-" + terminal["request_sha256"]
        sidecar = run / SIDECAR_NAME
        if not sidecar.exists():
            summary = summarize(run, session)
            if len(json.dumps(summary).encode("utf-8")) > MAX_SIDECAR_BYTES:
                return "kept_oversized"
            from . import embedded_nokiy as caller
            caller._write_create_only(sidecar, summary)
        load(sidecar, session)
        shutil.rmtree(target)
        return "retired"
    except Exception:  # retention must never change a sealed result
        return "kept_unproven"

