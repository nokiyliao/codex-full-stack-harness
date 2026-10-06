use super::SessionLogStore;
use super::connection::{init_workspace_db, with_connection};
use super::helpers::{bounded_page, parse_json_field};
use super::payload::{
    IndexSessionRow, index_session_from_row, load_workspace_session_payload,
    load_workspace_session_summary_payload,
};
use crate::path::normalize_workspace;
use anyhow::Result;
use rusqlite::{OptionalExtension, params};
use session_log_contract::{
    ContextSlice, GetSessionRequest, ListRuntimeLocationsRequest, ListSessionRecordsRequest,
    ListSessionsRequest, Page, ReadContextSliceRequest, RuntimeLocation, SessionContextRecord,
    SessionRecord, SessionSnapshot, SessionSummary, WorkspaceSummary,
    EXECUTION_EVIDENCE_PAGE_BYTES, ExecutionEvidencePage, ExecutionEvidenceSnapshot,
    ExecutionEvidenceSummary, ReadExecutionEvidenceRequest, RuntimeEvidenceState,
    RuntimeEvidenceTotals, SortedObservedEvidence, observed_evidence_field,
};
use std::path::Path;

// Rust `str::trim` follows Unicode White_Space. SQLite's one-argument TRIM only removes U+0020,
// so bind the exact current White_Space set whenever SQL classifies a terminal evidence id.
const RUST_TRIM_WHITESPACE: &str = "\u{0009}\u{000a}\u{000b}\u{000c}\u{000d}\u{0020}\u{0085}\u{00a0}\u{1680}\u{2000}\u{2001}\u{2002}\u{2003}\u{2004}\u{2005}\u{2006}\u{2007}\u{2008}\u{2009}\u{200a}\u{2028}\u{2029}\u{202f}\u{205f}\u{3000}";
const COMPLETE_TERMINAL_PROOF_PREDICATE: &str = "terminal_proven = 1
                       AND terminal_revision IS NOT NULL
                       AND terminal_event_seq IS NOT NULL
                       AND terminal_evidence_id IS NOT NULL
                       AND TRIM(terminal_evidence_id, ?1) != ''";

impl SessionLogStore {
    pub fn list_runtime_locations(
        &self,
        request: ListRuntimeLocationsRequest,
    ) -> Result<(Page, Vec<RuntimeLocation>)> {
        let page_size = request.page_size.clamp(1, 500);
        self.with_index_connection(|conn| {
            let total = conn.query_row(
                &format!(
                    "SELECT COUNT(*) FROM runtime_locations WHERE NOT ({COMPLETE_TERMINAL_PROOF_PREDICATE})"
                ),
                params![RUST_TRIM_WHITESPACE],
                |row| row.get::<_, u64>(0),
            )?;
            let after_runtime_id = request.after_runtime_id.as_deref();
            let (page, locations) = if let Some(after_runtime_id) = after_runtime_id {
                let mut statement = conn.prepare(&format!(
                    "SELECT runtime_id, session_id, workspace_db_path, terminal_proven,
                            terminal_revision, terminal_event_seq, terminal_evidence_id
                     FROM runtime_locations
                     WHERE NOT ({COMPLETE_TERMINAL_PROOF_PREDICATE})
                       AND runtime_id > ?2
                     ORDER BY runtime_id ASC
                     LIMIT ?3"
                ))?;
                let locations = statement
                    .query_map(
                        params![RUST_TRIM_WHITESPACE, after_runtime_id, page_size],
                        runtime_location_from_row,
                    )?
                    .collect::<std::result::Result<Vec<_>, _>>()?;
                (0, locations)
            } else {
                let page = bounded_page(request.page, page_size, total, false);
                let mut statement = conn.prepare(&format!(
                    "SELECT runtime_id, session_id, workspace_db_path, terminal_proven,
                            terminal_revision, terminal_event_seq, terminal_evidence_id
                     FROM runtime_locations
                     WHERE NOT ({COMPLETE_TERMINAL_PROOF_PREDICATE})
                     ORDER BY runtime_id ASC
                     LIMIT ?2 OFFSET ?3"
                ))?;
                let locations = statement
                    .query_map(
                        params![
                            RUST_TRIM_WHITESPACE,
                            page_size,
                            page.saturating_mul(page_size)
                        ],
                        runtime_location_from_row,
                    )?
                    .collect::<std::result::Result<Vec<_>, _>>()?;
                (page, locations)
            };
            Ok((
                Page {
                    page,
                    page_size,
                    total,
                },
                locations,
            ))
        })
    }

    pub fn read_context_slice(&self, request: ReadContextSliceRequest) -> Result<ContextSlice> {
        if request.max_estimated_tokens == 0 {
            anyhow::bail!("context token budget must be greater than zero");
        }
        let workspace_db_path = self
            .workspace_db_path_for_session(&request.session_id)?
            .ok_or_else(|| anyhow::anyhow!("session {} not found", request.session_id))?;
        self.with_workspace_connection(&workspace_db_path, |conn| {
            let (next_sequence, retained_from_sequence, next_management_sequence) = conn
                .query_row(
                    "SELECT next_context_sequence, retained_from_sequence, next_management_sequence
                 FROM sessions WHERE session_id = ?1",
                    params![request.session_id],
                    |row| {
                        Ok((
                            row.get::<_, u64>(0)?,
                            row.get::<_, u64>(1)?,
                            row.get::<_, u64>(2)?,
                        ))
                    },
                )?;
            let mut statement = conn.prepare(
                "SELECT sequence, record_json FROM session_context_records
                 WHERE session_id = ?1 AND sequence >= ?2 AND sequence < ?3
                 ORDER BY sequence DESC",
            )?;
            let byte_budget = request.max_estimated_tokens.saturating_mul(4);
            let mut rows = statement.query(params![
                request.session_id,
                retained_from_sequence,
                next_sequence
            ])?;
            let mut selected_bytes = 0_u64;
            let mut records = Vec::new();
            while let Some(row) = rows.next()? {
                let raw_record = row.get::<_, String>(1)?;
                let record_bytes = raw_record.len() as u64;
                if !records.is_empty() && selected_bytes.saturating_add(record_bytes) > byte_budget
                {
                    break;
                }
                selected_bytes = selected_bytes.saturating_add(record_bytes);
                records.push(SessionContextRecord {
                    sequence: row.get(0)?,
                    raw_record,
                });
            }
            records.reverse();
            Ok(ContextSlice {
                records,
                retained_from_sequence,
                next_sequence,
                next_management_sequence,
            })
        })
    }

    pub fn read_execution_evidence(&self, request: ReadExecutionEvidenceRequest) -> Result<ExecutionEvidencePage> {
        request.validate().map_err(anyhow::Error::msg)?;
        let path = self.workspace_db_path_for_session(&request.session_id)?
            .ok_or_else(|| anyhow::anyhow!("session {} execution evidence is absent", request.session_id))?;
        self.with_workspace_connection(&path, |conn| {
            // Historical grouping may spill to SQLite's bounded file-backed
            // sorter; it must not accumulate the task trajectory in RAM.
            if request.include_summary { conn.pragma_update(None, "temp_store", "FILE")?; }
            // All metadata, preflight, summary and page rows share one SQLite read snapshot.
            let transaction = conn.unchecked_transaction()?;
            let snapshot = transaction.query_row(
                "SELECT session_id, next_context_sequence, next_management_sequence, retained_from_sequence
                 FROM sessions WHERE session_id = ?1", params![request.session_id], |row| {
                    Ok(ExecutionEvidenceSnapshot { session_id: row.get(0)?, next_sequence: row.get(1)?,
                        next_management_sequence: row.get(2)?, retained_from_sequence: row.get(3)? })
                })?;
            snapshot.validate().map_err(anyhow::Error::msg)?;
            if snapshot.session_id != request.session_id
                || request.snapshot.as_ref().is_some_and(|expected| expected != &snapshot)
                || request.from_sequence > snapshot.next_sequence {
                anyhow::bail!("session {} execution evidence snapshot/cursor drift", request.session_id);
            }
            if request.snapshot.is_none() || request.include_summary
                || request.from_sequence == snapshot.next_sequence {
                let (count, first, last, oversized, malformed) = transaction.query_row(
                    "SELECT COUNT(*), MIN(sequence), MAX(sequence),
                     COALESCE(MAX(length(CAST(record_json AS BLOB))), 0),
                     COALESCE(SUM(NOT json_valid(record_json)), 0)
                     FROM session_context_records WHERE session_id = ?1",
                    params![request.session_id], |row| Ok((row.get::<_, u64>(0)?,
                        row.get::<_, Option<u64>>(1)?, row.get::<_, Option<u64>>(2)?,
                        row.get::<_, u64>(3)?, row.get::<_, u64>(4)?)))?;
                if count != snapshot.next_sequence
                    || (count > 0 && (first != Some(0) || last != Some(count - 1))) {
                    anyhow::bail!("session {} execution evidence contains an absent prefix or sequence gap", request.session_id);
                }
                if oversized > EXECUTION_EVIDENCE_PAGE_BYTES || malformed != 0 {
                    anyhow::bail!("session {} execution evidence is malformed or exceeds the bounded record size", request.session_id);
                }
            }
            let summary = if request.include_summary {
                Some(execution_evidence_summary(&transaction, &request.session_id)?)
            } else { None };
            let mut statement = transaction.prepare(
                "SELECT sequence, length(CAST(record_json AS BLOB)), record_json
                 FROM session_context_records WHERE session_id = ?1 AND sequence >= ?2 AND sequence < ?3
                 ORDER BY sequence LIMIT ?4")?;
            let mut rows = statement.query(params![request.session_id, request.from_sequence,
                snapshot.next_sequence, request.max_records])?;
            let mut records = Vec::new();
            let mut bytes = 0_u64;
            let mut next_sequence = request.from_sequence;
            while let Some(row) = rows.next()? {
                let sequence: u64 = row.get(0)?;
                let size: u64 = row.get(1)?;
                if sequence != next_sequence { anyhow::bail!("execution evidence sequence gap at {next_sequence}"); }
                if size > request.max_bytes { anyhow::bail!("execution evidence record {sequence} exceeds page byte bound"); }
                if bytes + size > request.max_bytes { break; }
                records.push(SessionContextRecord { sequence, raw_record: row.get(2)? });
                bytes += size;
                next_sequence += 1;
            }
            let page = ExecutionEvidencePage { snapshot, next_sequence, records, summary };
            page.validate(&request).map_err(anyhow::Error::msg)?;
            drop(rows);
            drop(statement);
            transaction.commit()?;
            Ok(page)
        })
    }

    pub fn list_workspaces(&self) -> Result<Vec<WorkspaceSummary>> {
        self.with_index_connection(|conn| {
            let mut stmt = conn.prepare(
                "SELECT workspace, COUNT(*), COALESCE(MAX(updated_at), 0)
                 FROM sessions
                 WHERE workspace != ''
                 GROUP BY workspace
                 ORDER BY MAX(updated_at) DESC, workspace ASC",
            )?;
            let rows = stmt
                .query_map([], |row| {
                    Ok(WorkspaceSummary {
                        directory: row.get(0)?,
                        session_count: row.get::<_, i64>(1)? as u64,
                        last_updated_at: row.get(2)?,
                    })
                })?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            Ok(rows)
        })
    }

}

fn execution_evidence_summary(conn: &rusqlite::Connection, session_id: &str) -> Result<ExecutionEvidenceSummary> {
    let mut totals = RuntimeEvidenceTotals::default();
    visit_runtime_evidence_groups(conn, session_id, "runtime_id", |bound, state| {
        // An idempotent persistence replay has no duplicate sequence. A second
        // usage fact for the same runtime is not another billable/provider call.
        if bound && state.usage_records > 1 { anyhow::bail!("duplicate execution usage evidence for one runtime"); }
        totals.add(bound, state);
        Ok(())
    })?;
    let mut models = SortedObservedEvidence::default();
    let mut tiers = SortedObservedEvidence::default();
    visit_runtime_evidence_groups(conn, session_id, "model", |bound, state| {
        if bound && !state.observation_conflict {
            models.add(state.observation.as_ref().and_then(|value| value.1.as_deref())).map_err(anyhow::Error::msg)?;
        }
        Ok(())
    })?;
    visit_runtime_evidence_groups(conn, session_id, "service_tier", |bound, state| {
        if bound && !state.observation_conflict {
            tiers.add(state.observation.as_ref().and_then(|value| value.2.as_deref())).map_err(anyhow::Error::msg)?;
        }
        Ok(())
    })?;
    Ok(ExecutionEvidenceSummary { usage: totals.usage(), provider_observation:
        totals.provider_summary(models.summary(totals.runtime_count), tiers.summary(totals.runtime_count)) })
}

fn visit_runtime_evidence_groups(
    conn: &rusqlite::Connection, session_id: &str, order: &str,
    mut visit: impl FnMut(bool, &RuntimeEvidenceState) -> Result<()>,
) -> Result<()> {
    let order = match order { "runtime_id" => "g.runtime_id", "model" => "g.model, g.runtime_id",
        "service_tier" => "g.service_tier, g.runtime_id", _ => anyhow::bail!("invalid evidence grouping order") };
    // SQLite sorts/groups the existing canonical records; no duplicate ledger,
    // history-sized Vec, per-runtime map, or prompt restoration is introduced.
    let sql = format!("WITH evidence AS (
        SELECT sequence, record_json,
          COALESCE(CASE WHEN json_type(record_json, '$.runtime_id') = 'text'
            THEN trim(json_extract(record_json, '$.runtime_id'), ?2) END, '') AS runtime_id,
          CASE WHEN json_extract(record_json, '$.type') = 'runtime_provider_observation'
            THEN trim(json_extract(record_json, '$.provider_observation.model'), ?2) END AS model,
          CASE WHEN json_extract(record_json, '$.type') = 'runtime_provider_observation'
            THEN trim(json_extract(record_json, '$.provider_observation.service_tier'), ?2) END AS service_tier
        FROM session_context_records WHERE session_id = ?1
          AND json_extract(record_json, '$.type') IN ('runtime_usage', 'runtime_provider_observation')
      ), runtime_groups AS (
        SELECT runtime_id, MIN(model) AS model, MIN(service_tier) AS service_tier
        FROM evidence GROUP BY runtime_id
      ) SELECT e.record_json FROM runtime_groups g JOIN evidence e ON e.runtime_id = g.runtime_id
        ORDER BY {order}, e.sequence");
    let mut statement = conn.prepare(&sql)?;
    let mut rows = statement.query(params![session_id, RUST_TRIM_WHITESPACE])?;
    let mut current: Option<Option<String>> = None;
    let mut state = RuntimeEvidenceState::default();
    while let Some(row) = rows.next()? {
        let raw: String = row.get(0)?;
        let value: serde_json::Value = serde_json::from_str(&raw)?;
        if value.get("session_id").is_some_and(|id| id.as_str() != Some(session_id)) {
            anyhow::bail!("runtime execution evidence has wrong session identity");
        }
        let id = observed_evidence_field(&value, "runtime_id");
        if current.as_ref().is_some_and(|previous| previous != &id) {
            visit(current.as_ref().is_some_and(|id| id.is_some()), &state)?;
            state = RuntimeEvidenceState::default();
        }
        current = Some(id);
        state.add(&value);
    }
    if let Some(id) = current { visit(id.is_some(), &state)?; }
    Ok(())
}

impl SessionLogStore {
    pub fn list_sessions(
        &self,
        request: ListSessionsRequest,
    ) -> Result<(Page, Vec<SessionSnapshot>)> {
        let workspace = normalize_workspace(&request.workspace);
        let page_size = request.page_size.clamp(1, 500);
        let (page, total, index_rows) = self.with_index_connection(|conn| {
            let total = conn.query_row(
                "SELECT COUNT(*) FROM sessions WHERE workspace = ?1",
                params![workspace],
                |row| row.get::<_, i64>(0),
            )? as u64;
            let page = bounded_page(request.page, page_size, total, false);
            let mut stmt = conn.prepare(
                "SELECT session_id, workspace_db_path
                 FROM sessions
                 WHERE workspace = ?1
                 ORDER BY last_user_message_at DESC, session_id ASC
                 LIMIT ?2 OFFSET ?3",
            )?;
            let index_rows = stmt
                .query_map(
                    params![workspace, page_size as i64, (page * page_size) as i64],
                    index_session_from_row,
                )?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            Ok((page, total, index_rows))
        })?;
        let sessions = index_rows
            .into_iter()
            .filter_map(|row| match self.snapshot_from_index_row(row) {
                Ok(Some(snapshot)) => Some(Ok(snapshot)),
                Ok(None) => None,
                Err(error) => Some(Err(error)),
            })
            .collect::<Result<Vec<_>>>()?;
        Ok((
            Page {
                page,
                page_size,
                total,
            },
            sessions,
        ))
    }

    pub fn list_session_summaries(
        &self,
        request: ListSessionsRequest,
    ) -> Result<(Page, Vec<SessionSummary>)> {
        let workspace = normalize_workspace(&request.workspace);
        let page_size = request.page_size.clamp(1, 500);
        let (page, total, index_rows) = self.with_index_connection(|conn| {
            let total = conn.query_row(
                "SELECT COUNT(*) FROM sessions WHERE workspace = ?1",
                params![workspace],
                |row| row.get::<_, i64>(0),
            )? as u64;
            let page = bounded_page(request.page, page_size, total, false);
            let mut stmt = conn.prepare(
                "SELECT session_id, workspace_db_path
                 FROM sessions
                 WHERE workspace = ?1
                 ORDER BY last_user_message_at DESC, session_id ASC
                 LIMIT ?2 OFFSET ?3",
            )?;
            let index_rows = stmt
                .query_map(
                    params![workspace, page_size as i64, (page * page_size) as i64],
                    index_session_from_row,
                )?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            Ok((page, total, index_rows))
        })?;
        let sessions = index_rows
            .into_iter()
            .filter_map(|row| match self.summary_from_index_row(row) {
                Ok(Some(snapshot)) => Some(Ok(snapshot)),
                Ok(None) => None,
                Err(error) => Some(Err(error)),
            })
            .collect::<Result<Vec<_>>>()?;
        Ok((
            Page {
                page,
                page_size,
                total,
            },
            sessions,
        ))
    }

    pub fn get_session(&self, request: GetSessionRequest) -> Result<Option<SessionSnapshot>> {
        self.get_session_canonical(&request.session_id)
    }

    pub(super) fn get_session_canonical(
        &self,
        session_id: &str,
    ) -> Result<Option<SessionSnapshot>> {
        let row = self.with_index_connection(|conn| {
            conn.query_row(
                "SELECT session_id, workspace_db_path
                 FROM sessions
                 WHERE session_id = ?1",
                params![session_id],
                index_session_from_row,
            )
            .optional()
            .map_err(Into::into)
        })?;
        row.map(|row| self.snapshot_from_index_row(row))
            .transpose()
            .map(Option::flatten)
    }

    pub fn list_session_records(
        &self,
        request: ListSessionRecordsRequest,
    ) -> Result<(Page, Vec<SessionRecord>)> {
        let workspace_db_path = self.with_index_connection(|conn| {
            conn.query_row(
                "SELECT workspace_db_path FROM sessions WHERE session_id = ?1",
                params![request.session_id],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(Into::into)
        })?;
        let Some(workspace_db_path) = workspace_db_path else {
            return Ok((Page::default(), Vec::new()));
        };
        if !Path::new(&workspace_db_path).exists() {
            self.delete_index_session(&request.session_id)?;
            return Ok((Page::default(), Vec::new()));
        }
        let page_size = request.page_size.clamp(1, 500);
        with_connection(Path::new(&workspace_db_path), init_workspace_db, |conn| {
            let total = conn.query_row(
                "SELECT COUNT(*) FROM session_records WHERE session_id = ?1",
                params![request.session_id],
                |row| row.get::<_, i64>(0),
            )? as u64;
            let page = bounded_page(request.page, page_size, total, true);
            let mut stmt = conn.prepare(
                "SELECT session_id, message_id, role, created_at, updated_at, record_json
                 FROM session_records
                 WHERE session_id = ?1
                 ORDER BY created_at ASC, id ASC
                 LIMIT ?2 OFFSET ?3",
            )?;
            let rows = stmt
                .query_map(
                    params![
                        request.session_id,
                        page_size as i64,
                        (page * page_size) as i64
                    ],
                    |row| {
                        let record_json: String = row.get(5)?;
                        Ok((
                            row.get::<_, String>(0)?,
                            row.get::<_, String>(1)?,
                            row.get::<_, String>(2)?,
                            row.get::<_, i64>(3)?,
                            row.get::<_, i64>(4)?,
                            record_json,
                        ))
                    },
                )?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            let records = rows
                .into_iter()
                .map(
                    |(session_id, message_id, role, created_at, updated_at, record_json)| {
                        Ok(SessionRecord {
                            record: parse_json_field(
                                &record_json,
                                "record_json",
                                Some(&session_id),
                            )?,
                            session_id,
                            message_id,
                            role,
                            created_at,
                            updated_at,
                        })
                    },
                )
                .collect::<Result<Vec<_>>>()?;
            Ok((
                Page {
                    page,
                    page_size,
                    total,
                },
                records,
            ))
        })
    }

    fn snapshot_from_index_row(&self, row: IndexSessionRow) -> Result<Option<SessionSnapshot>> {
        let workspace_payload =
            load_workspace_session_payload(&row.workspace_db_path, &row.session_id)?;
        let Some(workspace_payload) = workspace_payload else {
            self.delete_index_session(&row.session_id)?;
            return Ok(None);
        };
        let snapshot = SessionSnapshot {
            session_id: row.session_id,
            workspace: workspace_payload.workspace,
            name: workspace_payload.name,
            created_at: workspace_payload.created_at,
            updated_at: workspace_payload.updated_at,
            last_user_message_at: workspace_payload.last_user_message_at,
            message_count: workspace_payload.message_count as u64,
            lifecycle_projection: workspace_payload.lifecycle_projection,
            management: workspace_payload.management,
            metadata: workspace_payload.metadata,
            todos: workspace_payload.todos,
        };
        snapshot.validate().map_err(anyhow::Error::msg)?;
        Ok(Some(snapshot))
    }

    fn summary_from_index_row(&self, row: IndexSessionRow) -> Result<Option<SessionSummary>> {
        let workspace_payload =
            load_workspace_session_summary_payload(&row.workspace_db_path, &row.session_id)?;
        let Some(workspace_payload) = workspace_payload else {
            self.delete_index_session(&row.session_id)?;
            return Ok(None);
        };
        Ok(Some(SessionSummary {
            session_id: row.session_id,
            workspace: workspace_payload.workspace,
            name: workspace_payload.name,
            parent_id: workspace_payload.parent_id,
            created_at: workspace_payload.created_at,
            updated_at: workspace_payload.updated_at,
            last_user_message_at: workspace_payload.last_user_message_at,
            state: workspace_payload.state,
            status: workspace_payload.status,
            message_count: workspace_payload.message_count as u64,
            feed_cursor: workspace_payload.feed_cursor,
            task_management: workspace_payload.task_management,
            metadata: workspace_payload.metadata,
        }))
    }

    pub(super) fn delete_index_session(&self, session_id: &str) -> Result<()> {
        self.with_index_connection(|conn| {
            let tx = conn.transaction()?;
            tx.execute(
                "DELETE FROM runtime_locations WHERE session_id = ?1",
                params![session_id],
            )?;
            tx.execute(
                "DELETE FROM sessions WHERE session_id = ?1",
                params![session_id],
            )?;
            tx.commit()?;
            Ok(())
        })
    }
}

fn runtime_location_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<RuntimeLocation> {
    Ok(RuntimeLocation {
        runtime_id: row.get(0)?,
        session_id: row.get(1)?,
        workspace_db_path: row.get(2)?,
        terminal_proven: row.get(3)?,
        terminal_revision: row.get(4)?,
        terminal_event_seq: row.get(5)?,
        terminal_evidence_id: row.get(6)?,
    })
}
