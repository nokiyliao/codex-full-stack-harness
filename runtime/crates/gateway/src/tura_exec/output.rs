use std::collections::{BTreeMap, BTreeSet};
use std::cell::{Cell, RefCell};
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use runtime_contract::{ModelServiceTier, TerminalEvidenceProjection};
use serde_json::{Value, json};
use session_log_contract::{
    EXECUTION_EVIDENCE_PAGE_BYTES, EXECUTION_EVIDENCE_PAGE_RECORDS,
    ExecutionEvidencePage, ExecutionEvidenceReference, ExecutionEvidenceSnapshot,
    ExecutionEvidenceSummary, ReadExecutionEvidenceRequest, RuntimeEvidenceState,
    RuntimeEvidenceTotals, SessionLogCommand, SessionLogResponse,
    observed_evidence_field, visit_execution_evidence,
};

use super::cli::CliConfig;
use super::env::normalize_model;

pub(crate) fn write_last_message(path: &Path, text: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("failed to create last-message directory: {err}"))?;
    }
    fs::write(path, text).map_err(|err| format!("failed to write last message: {err}"))
}

pub(crate) fn write_jsonl(
    session_log: &[String],
    session_id: &str,
    config: &CliConfig,
    emit_thread_start: bool,
) -> Result<(), String> {
    if emit_thread_start {
        emit_cli_start_events(config, session_id)?;
    }

    if let Some(reference) = execution_evidence_reference(session_log)? {
        if reference.snapshot.session_id != session_id {
            return Err("execution output session identity mismatch".to_string());
        }
        let summary = execution_evidence_summary(&reference)?;
        EVIDENCE_ITEMS_ATTEMPTED.with(|attempted| attempted.set(true));
        project_execution_evidence_with(&reference, &config.cwd, true, cli_live_jsonl_enabled(),
            request_execution_evidence, emit_jsonl)?;
        let mut usage = evidence_usage(&reference, summary.usage);
        usage["_execution_evidence_items_projected"] = json!(true);
        emit_jsonl(&turn_completed_event(config, session_id, usage,
            summary.provider_observation, "completed", None))?;
        return io::stdout().flush().map_err(|error| format!("failed to flush stdout: {error}"));
    }

    let mut emit = emit_jsonl;
    project_context_log_with(session_log, session_id, &config.cwd, true,
        cli_live_jsonl_enabled(), &mut emit)?;

    let usage = aggregate_runtime_usage(session_log);
    let observation = aggregate_provider_observations(session_log);
    emit_jsonl(&turn_completed_event(
        config,
        session_id,
        usage,
        observation,
        "completed",
        None,
    ))?;
    io::stdout()
        .flush()
        .map_err(|err| format!("failed to flush stdout: {err}"))
}

fn emit_terminal_evidence(terminal: TerminalEvidenceProjection,
    emit: &mut impl FnMut(&Value) -> Result<(), String>) -> Result<(), String> {
    if let Some(value) = terminal.finish()? {
        // Emit the exact typed record, without synthetic assistant text or
        // extra reference metadata. Only a fully validated traversal can
        // reach this point; env flags and absent replies are not evidence.
        // Permit this exact validated record only for the duration of this
        // emission. The generic/live JSONL ingress must not accept markers
        // supplied by env, provider output, or an unvalidated caller.
        let previous = TERMINAL_EVIDENCE_READY.with(|ready| ready.replace(Some(value.clone())));
        let result = emit(&value);
        TERMINAL_EVIDENCE_READY.with(|ready| *ready.borrow_mut() = previous);
        result?;
    }
    Ok(())
}

fn project_context_log_with(log: &[String], session_id: &str, cwd: &Path,
    messages: bool, live: bool, emit: &mut impl FnMut(&Value) -> Result<(), String>) -> Result<(), String> {
    let mut terminal = TerminalEvidenceProjection::default();
    let mut item_index = 0;
    for entry in log {
        let value = match serde_json::from_str::<Value>(entry) {
            Ok(value) => value,
            Err(error) if entry.contains("nokiy.terminal_evidence") =>
                return Err(format!("malformed terminal evidence JSON: {error}")),
            Err(_) => continue,
        };
        terminal.observe(&value, session_id, entry)?;
        project_context_value(&value, &mut item_index, cwd, messages, live, emit)?;
    }
    emit_terminal_evidence(terminal, emit)
}

fn project_context_value(value: &Value, item_index: &mut usize, cwd: &Path,
    emit_messages: bool, live: bool, emit: &mut impl FnMut(&Value) -> Result<(), String>) -> Result<(), String> {
        if emit_messages && value.get("role").and_then(Value::as_str) == Some("assistant") {
            let text = value
                .get("content")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let text = clean_agent_message(text);
            if !text.trim().is_empty() {
                emit(&json!({
                    "type": "item.completed",
                    "item": {
                        "id": format!("item_{item_index}"),
                        "type": "agent_message",
                        "text": text
                    }
                }))?;
                *item_index += 1;
            }
        } else if value.get("type").and_then(Value::as_str) == Some("tool_result") {
            let tool_name = value
                .get("tool_name")
                .and_then(Value::as_str)
                .unwrap_or("tool");
            if tool_name == "command_run" {
                if emit_messages && *item_index == 0 {
                    let summary = value
                        .get("input")
                        .and_then(|input| input.get("step_summary"))
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .filter(|summary| !summary.is_empty())
                        .unwrap_or(
                            "I'll inspect the requested file first, then apply the patch and run verification.",
                        );
                    emit(&json!({
                        "type": "item.completed",
                        "item": {
                            "id": format!("item_{item_index}"),
                            "type": "agent_message",
                            "text": summary
                        }
                    }))?;
                    *item_index += 1;
                }
                if !live {
                    emit_command_run_events(value, item_index, cwd, emit)?;
                }
            }
        }
    Ok(())
}

pub(crate) fn emit_cli_start_events(config: &CliConfig, session_id: &str) -> Result<(), String> {
    emit_jsonl(&thread_started_event(config, session_id))?;
    emit_jsonl(&turn_started_event(config, session_id))
}

pub(crate) fn thread_started_event(config: &CliConfig, session_id: &str) -> Value {
    let mut event = cli_run_event(config, session_id, "thread.started");
    if let Some(object) = event.as_object_mut() {
        object.insert("thread_id".to_string(), json!(session_id));
    }
    event
}

pub(crate) fn turn_started_event(config: &CliConfig, session_id: &str) -> Value {
    cli_run_event(config, session_id, "turn.started")
}

pub(crate) fn turn_completed_event(
    config: &CliConfig,
    session_id: &str,
    usage: Value,
    provider_observation: Value,
    status: &str,
    error: Option<&str>,
) -> Value {
    let mut event = cli_run_event(config, session_id, "turn.completed");
    if let Some(object) = event.as_object_mut() {
        object.insert("status".to_string(), json!(status));
        object.insert("usage".to_string(), usage);
        object.insert("provider_observation".to_string(), provider_observation);
        if let Some(error) = error {
            object.insert("error".to_string(), json!(error));
        }
    }
    event
}

fn cli_run_event(config: &CliConfig, session_id: &str, event_type: &str) -> Value {
    let metadata = cli_run_metadata(config, session_id);
    let mut object = metadata
        .as_object()
        .cloned()
        .unwrap_or_else(serde_json::Map::new);
    object.insert("type".to_string(), json!(event_type));
    object.insert("metadata".to_string(), metadata);
    Value::Object(object)
}

fn cli_run_metadata(config: &CliConfig, session_id: &str) -> Value {
    let model = config
        .model
        .as_deref()
        .map(normalize_model)
        .or_else(|| env_nonempty("TURA_SESSION_MODEL_OVERRIDE"));
    let reasoning_effort = config
        .reasoning_effort
        .clone()
        .or_else(|| env_nonempty("TURA_SESSION_REASONING_EFFORT"));
    let service_tier = config.effective_service_tier();
    json!({
        "session_id": session_id,
        "cwd": config.cwd.to_string_lossy().to_string(),
        "agent": config.agent,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "service_tier": service_tier.as_str(),
        "priority": service_tier == ModelServiceTier::Priority,
        "acceleration_enabled": service_tier != ModelServiceTier::Default,
        "max_tokens": config.max_tokens,
    })
}

fn env_nonempty(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

pub(crate) fn write_turn_log_stderr(
    session_log: &[String],
    turn_started_at_ms: Option<i64>,
) -> Result<(), String> {
    if let Some(reference) = execution_evidence_reference(session_log)? {
        return write_execution_turn_log(&reference);
    }
    let summary = turn_log_summary(session_log, turn_started_at_ms);
    writeln!(
        io::stderr(),
        "TURA_TURN_LOG {}",
        serde_json::to_string(&summary)
            .map_err(|err| format!("failed to encode turn log: {err}"))?
    )
    .map_err(|err| format!("failed to write turn log to stderr: {err}"))
}

fn cli_live_jsonl_enabled() -> bool {
    std::env::var("TURA_CLI_LIVE_JSONL")
        .ok()
        .is_some_and(|value| {
            matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
}

pub(crate) fn aggregate_runtime_usage(session_log: &[String]) -> Value {
    match execution_evidence_reference(session_log) {
        Ok(Some(reference)) => return match execution_evidence_summary(&reference) {
            Ok(summary) => evidence_usage(&reference, summary.usage),
            Err(error) => json!({"_execution_evidence_error":error}),
        },
        Err(error) => return json!({"_execution_evidence_error":error}),
        Ok(None) => {}
    }
    let mut runtimes: BTreeMap<String, RuntimeEvidenceState> = BTreeMap::new();
    let mut totals = RuntimeEvidenceTotals::default();
    for entry in session_log {
        let Ok(value) = serde_json::from_str::<Value>(entry) else {
            totals.unbound_or_malformed = true;
            continue;
        };
        let kind = value.get("type").and_then(Value::as_str);
        if !matches!(kind, Some("runtime_usage" | "runtime_provider_observation")) {
            continue;
        }
        if let Some(id) = observed_evidence_field(&value, "runtime_id") {
            runtimes.entry(id).or_default().add(&value);
        } else {
            let mut state = RuntimeEvidenceState::default();
            state.add(&value);
            totals.add(false, &state);
        }
    }
    for state in runtimes.values() { totals.add(true, state); }
    totals.usage()
}

const MAX_OBSERVED_VALUES: usize = 8;

/// Bind observation records to unique runtime IDs; repeated or contradictory projections
/// cannot turn partial evidence into a unanimous observed value.
pub(crate) fn aggregate_provider_observations(session_log: &[String]) -> Value {
    match execution_evidence_reference(session_log) {
        Ok(Some(reference)) => return match execution_evidence_summary(&reference) {
            Ok(summary) => summary.provider_observation,
            Err(error) => json!({"_execution_evidence_error":error}),
        },
        Err(error) => return json!({"_execution_evidence_error":error}),
        Ok(None) => {}
    }
    let mut runtimes: BTreeMap<String, RuntimeEvidenceState> = BTreeMap::new();
    for entry in session_log {
        let Ok(value) = serde_json::from_str::<Value>(entry) else {
            continue;
        };
        let kind = value.get("type").and_then(Value::as_str);
        if !matches!(kind, Some("runtime_usage" | "runtime_provider_observation")) {
            continue;
        }
        let Some(id) = observed_evidence_field(&value, "runtime_id") else {
            continue;
        };
        runtimes.entry(id).or_default().add(&value);
    }

    let total = runtimes.len();
    let observed = runtimes
        .values()
        .filter(|state| state.observation.is_some() && !state.observation_conflict)
        .count();
    let conflicts = runtimes.values().filter(|state| state.observation_conflict).count();
    let summarize =
        |select: fn(&(Option<String>, Option<String>, Option<String>)) -> &Option<String>| {
            let values = runtimes
                .values()
                .filter_map(|state| if state.observation_conflict { None } else { state.observation.as_ref() })
                .filter_map(|entry| select(entry).as_ref())
                .collect::<Vec<_>>();
            let distinct = values
                .iter()
                .map(|value| value.to_string())
                .collect::<BTreeSet<_>>();
            let unanimous = if total > 0 && values.len() == total && distinct.len() == 1 {
                distinct.iter().next().cloned()
            } else {
                None
            };
            json!({"value": unanimous, "observed_count": values.len(),
            "distinct_count": distinct.len(),
            "distinct_values": distinct.into_iter().take(MAX_OBSERVED_VALUES).collect::<Vec<_>>()})
        };
    json!({"schema_version": "provider_observation_summary_v1", "source": "provider_response",
        "runtime_count": total, "observation_count": observed, "conflict_count": conflicts,
        "model": summarize(|entry| &entry.1), "service_tier": summarize(|entry| &entry.2)})
}

thread_local! {
    // Only the last bounded summary is cached, never raw trajectory records.
    static EVIDENCE_SUMMARY: RefCell<Option<(ExecutionEvidenceSnapshot, Result<ExecutionEvidenceSummary, String>)>> = const { RefCell::new(None) };
    static EVIDENCE_ITEMS_ATTEMPTED: Cell<bool> = const { Cell::new(false) };
    static CLI_TERMINAL_WRITTEN: Cell<bool> = const { Cell::new(false) };
    static TERMINAL_EVIDENCE_READY: RefCell<Option<Value>> = const { RefCell::new(None) };
    static TERMINAL_EVIDENCE_WRITTEN: RefCell<Option<Value>> = const { RefCell::new(None) };
}

fn execution_evidence_reference(log: &[String]) -> Result<Option<ExecutionEvidenceReference>, String> {
    for entry in log {
        let Ok(value) = serde_json::from_str::<Value>(entry) else { continue; };
        if value.get("type").and_then(Value::as_str) != Some("execution_evidence_v1") { continue; }
        if log.len() != 1 { return Err("execution evidence reference is mixed with a prompt tail".to_string()); }
        let reference: ExecutionEvidenceReference = serde_json::from_value(value).map_err(|error| error.to_string())?;
        reference.validate()?;
        return Ok(Some(reference));
    }
    Ok(None)
}

fn read_execution_evidence(request: ReadExecutionEvidenceRequest) -> Result<ExecutionEvidencePage, String> {
    let page = request_execution_evidence(request.clone())?;
    page.validate(&request)?;
    Ok(page)
}

// Paged traversal owns page validation, avoiding a second JSON parse of every
// raw record. Standalone snapshot/summary reads use the checked wrapper above.
fn request_execution_evidence(request: ReadExecutionEvidenceRequest) -> Result<ExecutionEvidencePage, String> {
    request.validate()?;
    let response = session_log_contract::client::call_service(&SessionLogCommand::ReadExecutionEvidence(request.clone()))
        .map_err(|error| format!("execution evidence read failed: {error}"))?;
    match response {
        SessionLogResponse::ExecutionEvidence { evidence } => Ok(evidence),
        SessionLogResponse::Error { error } => Err(format!("execution evidence unavailable: {error}")),
        other => Err(format!("unexpected execution evidence response: {other:?}")),
    }
}

fn execution_evidence_summary(reference: &ExecutionEvidenceReference) -> Result<ExecutionEvidenceSummary, String> {
    reference.validate()?;
    if let Some(summary) = EVIDENCE_SUMMARY.with(|cache| cache.borrow().as_ref()
        .filter(|(snapshot, _)| snapshot == &reference.snapshot).map(|(_, summary)| summary.clone())) {
        return summary;
    }
    let result = read_execution_evidence(ReadExecutionEvidenceRequest {
        session_id: reference.snapshot.session_id.clone(), snapshot: Some(reference.snapshot.clone()),
        from_sequence: reference.snapshot.next_sequence, max_records: EXECUTION_EVIDENCE_PAGE_RECORDS,
        max_bytes: EXECUTION_EVIDENCE_PAGE_BYTES, include_summary: true,
    }).and_then(|page| page.summary.ok_or_else(|| "execution evidence summary is absent".to_string()));
    EVIDENCE_SUMMARY.with(|cache| *cache.borrow_mut() = Some((reference.snapshot.clone(), result.clone())));
    result
}

fn evidence_usage(reference: &ExecutionEvidenceReference, mut usage: Value) -> Value {
    usage["_execution_evidence"] = json!(reference);
    usage
}

fn project_execution_evidence(reference: &ExecutionEvidenceReference, cwd: &Path, messages: bool) -> Result<(), String> {
    EVIDENCE_ITEMS_ATTEMPTED.with(|attempted| attempted.set(true));
    let live = cli_live_jsonl_enabled();
    project_execution_evidence_with(reference, cwd, messages, live, request_execution_evidence, emit_jsonl)
}

fn project_execution_evidence_with(
    reference: &ExecutionEvidenceReference, cwd: &Path, messages: bool, live: bool,
    read: impl FnMut(ReadExecutionEvidenceRequest) -> Result<ExecutionEvidencePage, String>,
    mut emit: impl FnMut(&Value) -> Result<(), String>,
) -> Result<(), String> {
    reference.validate()?;
    let mut terminal = TerminalEvidenceProjection::default();
    let mut item_index = 0;
    visit_execution_evidence(&reference.snapshot, read, |record| {
        let value: Value = serde_json::from_str(&record.raw_record).map_err(|error| error.to_string())?;
        terminal.observe(&value, &reference.snapshot.session_id, &record.raw_record)?;
        // Live command/file events have already been emitted once. They are
        // still traversed/validated, but are never replayed or counted as usage.
        let mut bound_emit = |event: &Value| {
            let mut event = event.clone();
            event["execution_evidence"] = json!({"session_id":reference.snapshot.session_id, "sequence":record.sequence});
            emit(&event)
        };
        project_context_value(&value, &mut item_index, cwd, messages, live, &mut bound_emit)
    })?;
    emit_terminal_evidence(terminal, &mut emit)
}

fn write_execution_turn_log(reference: &ExecutionEvidenceReference) -> Result<(), String> {
    let summary = execution_evidence_summary(reference)?;
    let mut output = io::stderr().lock();
    let encode = |output: &mut dyn Write, value: &Value| serde_json::to_writer(output, value).map_err(|error| error.to_string());
    write!(output, "TURA_TURN_LOG {{\"type\":\"turn.log\",\"scope\":\"complete_session_execution_evidence\",\"usage\":")
        .map_err(|error| error.to_string())?;
    encode(&mut output, &summary.usage)?;
    write!(output, ",\"provider_observation\":").map_err(|error| error.to_string())?;
    encode(&mut output, &summary.provider_observation)?;
    write!(output, ",\"tool_calls\":[").map_err(|error| error.to_string())?;
    let mut first = true;
    let mut started = None::<i64>;
    let mut finished = None::<i64>;
    visit_execution_evidence(&reference.snapshot, request_execution_evidence, |record| {
        let value: Value = serde_json::from_str(&record.raw_record).map_err(|error| error.to_string())?;
        if let Some(time) = log_entry_millis(&value) {
            started = Some(started.map_or(time, |previous| previous.min(time)));
            finished = Some(finished.map_or(time, |previous| previous.max(time)));
        }
        if let Some(tool) = tool_log_entry(&value) {
            if !first { write!(output, ",").map_err(|error| error.to_string())?; }
            first = false;
            encode(&mut output, &tool)?;
        }
        Ok(())
    })?;
    write!(output, "],\"text\":[").map_err(|error| error.to_string())?;
    first = true;
    visit_execution_evidence(&reference.snapshot, request_execution_evidence, |record| {
        let value: Value = serde_json::from_str(&record.raw_record).map_err(|error| error.to_string())?;
        if let Some(text) = text_log_entry(&value) {
            if !first { write!(output, ",").map_err(|error| error.to_string())?; }
            first = false;
            encode(&mut output, &text)?;
        }
        Ok(())
    })?;
    write!(output, "],\"timing\":").map_err(|error| error.to_string())?;
    let started = started.unwrap_or_default();
    let finished = finished.unwrap_or(started);
    encode(&mut output, &json!({"started_at_ms":started, "finished_at_ms":finished,
        "duration_ms":finished.saturating_sub(started), "provider_latency_ms":json_u64(&summary.usage, "latency_ms")}))?;
    writeln!(output, "}}").map_err(|error| error.to_string())
}

fn turn_log_summary(session_log: &[String], turn_started_at_ms: Option<i64>) -> Value {
    let entries = current_turn_values(session_log, turn_started_at_ms);
    let entry_strings = entries.iter().map(Value::to_string).collect::<Vec<_>>();
    let usage = aggregate_runtime_usage(&entry_strings);
    let timing = turn_timing(&entries, &usage);
    let tools = entries
        .iter()
        .filter_map(tool_log_entry)
        .collect::<Vec<_>>();
    let text = entries
        .iter()
        .filter_map(text_log_entry)
        .collect::<Vec<_>>();

    json!({
        "type": "turn.log",
        "usage": usage,
        "timing": timing,
        "tool_calls": tools,
        "text": text,
    })
}

fn current_turn_values(session_log: &[String], turn_started_at_ms: Option<i64>) -> Vec<Value> {
    let values = session_log
        .iter()
        .filter_map(|entry| serde_json::from_str::<Value>(entry).ok())
        .collect::<Vec<_>>();
    if let Some(started_at) = turn_started_at_ms {
        let filtered = values
            .iter()
            .filter(|value| log_entry_millis(value).is_none_or(|millis| millis >= started_at))
            .cloned()
            .collect::<Vec<_>>();
        if !filtered.is_empty() {
            return filtered;
        }
    }
    let start = values
        .iter()
        .rposition(|value| value.get("role").and_then(Value::as_str) == Some("user"))
        .unwrap_or(0);
    values.into_iter().skip(start).collect()
}

fn turn_timing(entries: &[Value], usage: &Value) -> Value {
    let mut times = entries
        .iter()
        .filter_map(log_entry_millis)
        .collect::<Vec<_>>();
    times.sort_unstable();
    let started_at_ms = times.first().copied().unwrap_or_default();
    let finished_at_ms = times.last().copied().unwrap_or(started_at_ms);
    json!({
        "started_at_ms": started_at_ms,
        "finished_at_ms": finished_at_ms,
        "duration_ms": finished_at_ms.saturating_sub(started_at_ms),
        "provider_latency_ms": json_u64(usage, "latency_ms"),
    })
}

fn log_entry_millis(value: &Value) -> Option<i64> {
    value
        .get("updated_at")
        .and_then(Value::as_i64)
        .or_else(|| value.get("created_at").and_then(Value::as_i64))
        .or_else(|| value.get("timestamp").and_then(timestamp_millis))
}

fn timestamp_millis(value: &Value) -> Option<i64> {
    let text = value.as_str()?;
    DateTime::parse_from_rfc3339(text)
        .ok()
        .map(|timestamp| timestamp.with_timezone(&Utc).timestamp_millis())
}

fn tool_log_entry(value: &Value) -> Option<Value> {
    if value.get("type").and_then(Value::as_str) != Some("tool_result") {
        return None;
    }
    Some(json!({
        "tool_name": value.get("tool_name").cloned().unwrap_or(Value::Null),
        "runtime_id": value.get("runtime_id").cloned().unwrap_or(Value::Null),
        "success": value.get("success").cloned().unwrap_or(Value::Null),
        "error": value.get("error").cloned().unwrap_or(Value::Null),
        "input": value.get("input").cloned().unwrap_or(Value::Null),
        "output": value.get("output").cloned().unwrap_or(Value::Null),
        "timestamp": value.get("timestamp").cloned().unwrap_or(Value::Null),
    }))
}

fn text_log_entry(value: &Value) -> Option<Value> {
    let role = value.get("role").and_then(Value::as_str)?;
    if !matches!(role, "user" | "assistant") {
        return None;
    }
    let content = text_content(value.get("content")?)?;
    let content = if role == "assistant" {
        clean_agent_message(&content)
    } else {
        content.trim().to_string()
    };
    if content.trim().is_empty() {
        return None;
    }
    Some(json!({
        "role": role,
        "runtime_id": value.get("runtime_id").cloned().unwrap_or(Value::Null),
        "content": content,
        "timestamp": value.get("timestamp").cloned().unwrap_or(Value::Null),
    }))
}

fn text_content(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text.to_string()),
        Value::Array(parts) => {
            let text = parts
                .iter()
                .filter_map(|part| {
                    part.get("text")
                        .or_else(|| part.get("content"))
                        .and_then(Value::as_str)
                })
                .collect::<Vec<_>>()
                .join("");
            (!text.trim().is_empty()).then_some(text)
        }
        _ => None,
    }
}

fn json_u64(value: &Value, key: &str) -> u64 {
    value.get(key).and_then(Value::as_u64).unwrap_or(0)
}

fn emit_command_run_events(
    value: &Value,
    item_index: &mut usize,
    cwd: &Path,
    emit: &mut impl FnMut(&Value) -> Result<(), String>,
) -> Result<(), String> {
    for result in flatten_command_results(
        value.get("output").unwrap_or(&Value::Null),
        value.get("input").unwrap_or(&Value::Null),
    ) {
        let command_type = result
            .get("command_type")
            .or_else(|| result.get("command"))
            .and_then(Value::as_str);
        if command_type == Some("apply_patch") {
            emit_file_change_event(&result, item_index, cwd, emit)?;
            continue;
        }
        let command = display_command(&result);
        emit(&json!({
            "type": "item.completed",
            "item": {
                "id": format!("item_{}", *item_index),
                "type": "command_execution",
                "command": command,
                "aggregated_output": command_output(&result),
                "exit_code": result.get("exit_code").and_then(Value::as_i64),
                "status": if result.get("success").and_then(Value::as_bool).unwrap_or(false) { "completed" } else { "failed" }
            }
        }))?;
        *item_index += 1;
    }
    Ok(())
}

fn emit_file_change_event(
    result: &Value,
    item_index: &mut usize,
    cwd: &Path,
    emit: &mut impl FnMut(&Value) -> Result<(), String>,
) -> Result<(), String> {
    let changes = file_changes(result, cwd);
    emit(&json!({
        "type": "item.completed",
        "item": {
            "id": format!("item_{}", *item_index),
            "type": "file_change",
            "changes": changes,
            "status": if result.get("success").and_then(Value::as_bool).unwrap_or(false) { "completed" } else { "failed" }
        }
    }))?;
    *item_index += 1;
    Ok(())
}

fn file_changes(result: &Value, cwd: &Path) -> Vec<Value> {
    let mut changes = Vec::new();
    for change in result
        .get("response")
        .and_then(|value| value.get("changes"))
        .or_else(|| result.get("changes"))
        .or_else(|| result.get("output").and_then(|value| value.get("changes")))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let Some(raw_path) = change.get("path").and_then(Value::as_str) else {
            continue;
        };
        let kind = change
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or("update");
        let path = PathBuf::from(raw_path);
        let display_path = if path.is_absolute() {
            path
        } else {
            cwd.join(path)
        };
        changes.push(json!({
            "path": display_path.to_string_lossy().to_string(),
            "kind": kind
        }));
    }
    if changes.is_empty() {
        changes.push(json!({
            "path": cwd.to_string_lossy().to_string(),
            "kind": "update"
        }));
    }
    changes
}

fn flatten_command_results(output: &Value, input: &Value) -> Vec<Value> {
    let mut values = Vec::new();
    let output = output.get("streamed_command_run_result").unwrap_or(output);
    let input_commands = input
        .get("commands")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if let Some(runs) = output.get("results").and_then(Value::as_array) {
        for (index, run) in runs.iter().enumerate() {
            let mut run = run.clone();
            if let (Some(object), Some(input_command)) =
                (run.as_object_mut(), input_commands.get(index))
            {
                if let Some(command_type) = input_command
                    .get("command_type")
                    .or_else(|| input_command.get("command"))
                    .cloned()
                {
                    object
                        .entry("command_type".to_string())
                        .or_insert(command_type);
                }
                if let Some(command_line) = input_command.get("command_line").cloned() {
                    object
                        .entry("command_line".to_string())
                        .or_insert(command_line);
                }
            }
            if let Some(nested) = run.get("results").and_then(Value::as_array) {
                values.extend(nested.iter().cloned());
            } else {
                values.push(run);
            }
        }
    }
    if values.is_empty() {
        values.push(output.clone());
    }
    values
}

fn display_command(result: &Value) -> String {
    let command_type = result
        .get("command_type")
        .or_else(|| result.get("command"))
        .and_then(Value::as_str);
    let command = result
        .get("display_command")
        .or_else(|| result.get("command_line"))
        .or_else(|| result.get("command"))
        .and_then(Value::as_str)
        .unwrap_or("command_run")
        .to_string();
    if command_type == Some("shell_command") {
        return display_shell_command(&command);
    }
    command
}

fn display_shell_command(command: &str) -> String {
    let escaped = command.replace('\'', "''");
    if cfg!(windows) {
        format!("{} -Command '{escaped}'", quoted_powershell_path())
    } else {
        format!("/bin/bash -lc '{escaped}'")
    }
}

fn quoted_powershell_path() -> String {
    let preferred = PathBuf::from(r"C:\Program Files\PowerShell\7\pwsh.exe");
    if preferred.exists() {
        return format!("\"{}\"", preferred.to_string_lossy());
    }
    "\"pwsh.exe\"".to_string()
}

fn command_output(result: &Value) -> String {
    let has_diagnostic = ["error", "stderr"].iter().any(|key| {
        result
            .get(*key)
            .and_then(Value::as_str)
            .is_some_and(|text| !text.trim().is_empty())
    });
    if !has_diagnostic {
        if let Some(text) = result.get("stdout").and_then(Value::as_str) {
            return text.to_string();
        }
        if let Some(text) = result.get("output").and_then(Value::as_str) {
            return shell_display_output(text).to_string();
        }
        return result
            .get("output")
            .map(|value| serde_json::to_string(value).unwrap_or_default())
            .unwrap_or_default();
    }
    if let Some(text) = result
        .get("stdout")
        .and_then(Value::as_str)
        .filter(|text| !text.trim().is_empty())
    {
        return text.to_string();
    }
    if let Some(text) = result.get("output").and_then(Value::as_str) {
        let displayed = shell_display_output(text);
        if !displayed.trim().is_empty() {
            return displayed.to_string();
        }
    }
    if let Some(value) = result
        .get("output")
        .filter(|value| !value.is_null() && !value.is_string())
    {
        return serde_json::to_string(value).unwrap_or_default();
    }
    if let Some(error) = result
        .get("error")
        .and_then(Value::as_str)
        .filter(|error| !error.trim().is_empty())
    {
        return error.to_string();
    }
    if let Some(stderr) = result
        .get("stderr")
        .and_then(Value::as_str)
        .filter(|stderr| !stderr.trim().is_empty())
    {
        return stderr.to_string();
    }
    String::new()
}

fn shell_display_output(text: &str) -> &str {
    let Some(after_output) = text.split_once("\nOutput:\n").map(|(_, output)| output) else {
        return text;
    };
    if text.starts_with("Exit code: ") && text.contains("\nWall time: ") {
        return after_output;
    }
    text
}

fn clean_agent_message(text: &str) -> String {
    let trimmed = text.trim();
    if trimmed.is_empty() || looks_like_tool_payload(trimmed) {
        return String::new();
    }
    if let Some(index) = trimmed.find("{\"commands\"") {
        let (prefix, suffix) = trimmed.split_at(index);
        if looks_like_tool_payload(suffix) {
            return prefix.trim().to_string();
        }
    }
    trimmed.to_string()
}

fn looks_like_tool_payload(text: &str) -> bool {
    let trimmed = text.trim_start();
    if !trimmed.starts_with('{') {
        return false;
    }
    trimmed.contains("\"commands\"")
        || trimmed.contains("\"step_summary\"")
        || trimmed.contains("\"tool_calls\"")
        || trimmed.contains("\"reply_message\"")
}

pub(crate) fn emit_jsonl(value: &Value) -> Result<(), String> {
    if value["type"] == "nokiy.terminal_evidence"
        || value["schema_version"] == "nokiy_terminal_evidence_v1"
    {
        if !TERMINAL_EVIDENCE_READY.with(|ready| ready.borrow().as_ref() == Some(value)) {
            return Err("terminal evidence emission requires validated persisted evidence".to_string());
        }
        if let Some(previous) = TERMINAL_EVIDENCE_WRITTEN.with(|written| written.borrow().clone()) {
            return if previous == *value {
                Ok(()) // Live terminal projection and final output share one event.
            } else {
                Err("conflicting terminal evidence emission".to_string())
            };
        }
        print_jsonl(value)?;
        TERMINAL_EVIDENCE_WRITTEN.with(|written| *written.borrow_mut() = Some(value.clone()));
        return Ok(());
    }
    if value.get("type").and_then(Value::as_str) == Some("thread.started") {
        EVIDENCE_ITEMS_ATTEMPTED.with(|attempted| attempted.set(false));
        CLI_TERMINAL_WRITTEN.with(|written| written.set(false));
        EVIDENCE_SUMMARY.with(|summary| *summary.borrow_mut() = None);
        TERMINAL_EVIDENCE_READY.with(|ready| *ready.borrow_mut() = None);
        TERMINAL_EVIDENCE_WRITTEN.with(|written| *written.borrow_mut() = None);
    }
    if value.get("type").and_then(Value::as_str) == Some("turn.completed") {
        for field in ["usage", "provider_observation"] {
            if let Some(error) = value[field]["_execution_evidence_error"].as_str() {
                return Err(error.to_string());
            }
        }
        if let Some(reference) = value["usage"].get("_execution_evidence") {
            let reference: ExecutionEvidenceReference = serde_json::from_value(reference.clone()).map_err(|error| error.to_string())?;
            reference.validate()?;
            if value.get("session_id").and_then(Value::as_str) != Some(reference.snapshot.session_id.as_str()) {
                return Err("terminal execution evidence session identity mismatch".to_string());
            }
            if value["usage"]["_execution_evidence_items_projected"].as_bool() == Some(true) {
                let _ = read_execution_evidence(ReadExecutionEvidenceRequest {
                    session_id: reference.snapshot.session_id.clone(), snapshot: Some(reference.snapshot.clone()),
                    from_sequence: reference.snapshot.next_sequence, max_records: EXECUTION_EVIDENCE_PAGE_RECORDS,
                    max_bytes: EXECUTION_EVIDENCE_PAGE_BYTES, include_summary: false,
                })?;
            } else {
                let cwd = value.get("cwd").and_then(Value::as_str).ok_or("execution output cwd is absent")?;
                project_execution_evidence(&reference, Path::new(cwd), false)?;
            }
            let mut value = value.clone();
            if let Some(usage) = value["usage"].as_object_mut() {
                usage.remove("_execution_evidence");
                usage.remove("_execution_evidence_items_projected");
            }
            value["execution_evidence"] = json!({"schema_version":"execution_evidence_v1",
                "session_id":reference.snapshot.session_id, "from_sequence":0,
                "next_sequence":reference.snapshot.next_sequence,
                "next_management_sequence":reference.snapshot.next_management_sequence, "status":"complete",
                "command_projection":if cli_live_jsonl_enabled() {"live_not_replayed"} else {"paged"}});
            return print_jsonl(&value);
        }
    }
    print_jsonl(value)
}

fn print_jsonl(value: &Value) -> Result<(), String> {
    println!(
        "{}",
        serde_json::to_string(value).map_err(|err| format!("failed to encode jsonl: {err}"))?
    );
    if value.get("type").and_then(Value::as_str) == Some("turn.completed") {
        CLI_TERMINAL_WRITTEN.with(|written| written.set(true));
    }
    Ok(())
}

pub(crate) fn write_failed_jsonl(config: &CliConfig, session_id: &str, error: &str) -> Result<(), String> {
    if CLI_TERMINAL_WRITTEN.with(Cell::get) { return Ok(()); }
    let mut usage = aggregate_runtime_usage(&[]);
    let mut observation = aggregate_provider_observations(&[]);
    let result = read_execution_evidence(ReadExecutionEvidenceRequest {
        session_id: session_id.to_string(), snapshot: None, from_sequence: 0,
        max_records: 1, max_bytes: EXECUTION_EVIDENCE_PAGE_BYTES, include_summary: true,
    });
    let evidence = match result {
        Ok(page) => {
            let reference = ExecutionEvidenceReference::new(page.snapshot, 0);
            let summary = page.summary.ok_or("failure execution evidence summary is absent")?;
            usage = summary.usage;
            observation = summary.provider_observation;
            let traversal = if EVIDENCE_ITEMS_ATTEMPTED.with(Cell::get) {
                visit_execution_evidence(&reference.snapshot, request_execution_evidence, |_| Ok(()))
            } else {
                project_execution_evidence(&reference, &config.cwd, false)
            };
            match traversal {
                Ok(()) => json!({"schema_version":"execution_evidence_v1", "session_id":session_id,
                    "from_sequence":0, "next_sequence":reference.snapshot.next_sequence,
                    "status":"persisted_prefix", "terminal_boundary_verified":false}),
                Err(read_error) => {
                    usage = aggregate_runtime_usage(&[]);
                    observation = aggregate_provider_observations(&[]);
                    json!({"status":"incomplete", "error":read_error})
                }
            }
        }
        Err(read_error) => json!({"status":"unavailable", "error":read_error}),
    };
    // The failed router path may discard the worker envelope; its exact final
    // cursor is then unknowable. Expose persisted facts, never claim completeness.
    usage["coverage"]["status"] = json!("incomplete");
    let mut event = turn_completed_event(config, session_id, usage, observation, "failed", Some(error));
    event["execution_evidence"] = evidence;
    print_jsonl(&event)
}

#[cfg(test)]
mod tests {
    use super::{
        aggregate_provider_observations, aggregate_runtime_usage, clean_agent_message,
        command_output, display_command, file_changes, flatten_command_results,
        shell_display_output, thread_started_event, turn_completed_event, turn_log_summary,
    };
    use crate::tura_exec::cli::CliConfig;
    use serde_json::{Value, json};
    use std::path::PathBuf;

    fn terminal_marker() -> Value {
        json!({"type":"nokiy.terminal_evidence", "schema_version":"nokiy_terminal_evidence_v1",
            "session_id":"terminal-projection", "runtime_id":"runtime-terminal",
            "terminal_status":"done", "delivery_mode":"evidence_only",
            "parent_acceptance_required":true, "final_summary_turn_executed":false})
    }

    fn terminal_projection_records() -> Vec<String> {
        vec![
            json!({"type":"runtime_provider_observation", "runtime_id":"runtime-terminal"}).to_string(),
            json!({"type":"runtime_usage", "runtime_id":"runtime-terminal",
                "usage":{"input_tokens":2,"output_tokens":1,"total_tokens":3}}).to_string(),
            json!({"role":"assistant", "content":"Already published assistant text."}).to_string(),
            json!({"type":"tool_result", "runtime_id":"runtime-terminal", "tool_name":"command_run", "success":true,
                "input":{"commands":[]}, "output":{"results":[
                {"command_type":"shell_command","command_line":"echo preserved","success":true,"stdout":"preserved","exit_code":0},
                    {"command_type":"task_status","success":true,"output":{"task_status":{"status":"done"}}}
                ]}}).to_string(),
        ]
    }

    fn project_terminal_fixture(records: &[String], paged: bool, live: bool, drift: bool)
        -> (Result<(), String>, Vec<Value>) {
        use session_log_contract::{ExecutionEvidencePage, ExecutionEvidenceReference, ExecutionEvidenceSnapshot, SessionContextRecord};
        let mut events = Vec::new();
        let mut emit = |event: &Value| { events.push(event.clone()); Ok(()) };
        let result = if paged {
            let reference = ExecutionEvidenceReference::new(ExecutionEvidenceSnapshot {
                session_id:"terminal-projection".to_string(), next_sequence:records.len() as u64,
                next_management_sequence:2, retained_from_sequence:records.len().saturating_sub(1) as u64,
            }, 100);
            super::project_execution_evidence_with(&reference, &PathBuf::from("/workspace"), true, live,
                |request| {
                    let mut snapshot = reference.snapshot.clone();
                    if drift && request.from_sequence == snapshot.next_sequence {
                        snapshot.next_management_sequence += 1;
                    }
                    let end = (request.from_sequence + 1).min(reference.snapshot.next_sequence);
                    Ok(ExecutionEvidencePage { snapshot, next_sequence:end,
                        records:(request.from_sequence..end).map(|sequence| SessionContextRecord {
                            sequence, raw_record:records[sequence as usize].clone(),
                        }).collect(), summary:None })
                }, &mut emit)
        } else {
            super::project_context_log_with(records, "terminal-projection", &PathBuf::from("/workspace"), true, live, &mut emit)
        };
        (result, events)
    }

    #[test]
    fn blocked_delivery_projects_exact_marker_and_preserves_command_failures() {
        let mut records = terminal_projection_records();
        records.insert(0, json!({"type":"tool_result", "runtime_id":"past-runtime", "tool_name":"command_run", "success":false,
            "input":{"commands":[]}, "output":{"results":[{"command_type":"shell_command", "command_line":"failed command",
                "success":false, "exit_code":7, "output":{"stderr":"original failure", "exit_code":7}}]}}).to_string());
        let mut marker = terminal_marker(); marker["terminal_status"] = json!("blocked");
        records.push(marker.to_string());
        for paged in [false, true] {
            let (result, events) = project_terminal_fixture(&records, paged, false, false);
            result.expect("completed delivery, not successful task");
            let markers: Vec<_> = events.iter().filter(|event| event["type"] == "nokiy.terminal_evidence").collect();
            assert_eq!(markers, vec![&marker]);
            assert!(events.iter().any(|event| event["item"]["status"] == "failed"
                && event["item"]["exit_code"] == 7));
        }
    }

    #[test]
    fn terminal_evidence_projects_exact_persisted_record_from_context_and_execution_pages() {
        let mut records = terminal_projection_records();
        records.push(terminal_marker().to_string());
        for paged in [false, true] {
            for live in [false, true] {
                let (result, events) = project_terminal_fixture(&records, paged, live, false);
                result.expect("bound persisted evidence");
                let markers: Vec<_> = events.iter().filter(|event| event["type"] == "nokiy.terminal_evidence").collect();
                assert_eq!(markers, vec![&terminal_marker()]);
                assert!(events.iter().any(|event| event["item"]["text"] == "Already published assistant text."));
                assert_eq!(events.iter().filter(|event| event["item"]["type"] == "command_execution").count(), if live { 0 } else { 2 });
                if !live {
                    assert!(events.iter().any(|event| event["item"]["aggregated_output"] == "preserved"));
                }
            }
        }
        assert_eq!(aggregate_runtime_usage(&records)["total_tokens"], 3);
    }

    #[test]
    fn terminal_evidence_rejects_malformed_foreign_duplicate_and_unsealed_markers() {
        let mut invalid = Vec::new();
        for (key, value) in [
            ("type", json!("other")), ("schema_version", json!("other")),
            ("session_id", json!("foreign-session")), ("runtime_id", json!("foreign-runtime")),
            ("runtime_id", Value::Null), ("terminal_status", json!("question")),
            ("delivery_mode", json!("assistant_reply")), ("parent_acceptance_required", json!(false)),
            ("final_summary_turn_executed", json!(true)), ("extra", json!(true)),
        ] {
            let mut marker = terminal_marker(); marker[key] = value;
            invalid.push(vec![marker.to_string()]);
        }
        let mut missing = terminal_marker();
        missing.as_object_mut().expect("marker").remove("runtime_id");
        invalid.push(vec![missing.to_string()]);
        invalid.push(vec!["{\"type\":\"nokiy.terminal_evidence\"".to_string()]);
        invalid.push(vec![terminal_marker().to_string(), terminal_marker().to_string()]);
        invalid.push(vec![terminal_marker().to_string().replace(
            "\"session_id\":\"terminal-projection\"",
            "\"session_id\":\"foreign-session\",\"session_id\":\"terminal-projection\"",
        )]);
        invalid.push(vec![terminal_marker().to_string(), json!({"type":"runtime_provider_observation", "runtime_id":"later"}).to_string()]);
        for suffix in invalid {
            let mut records = terminal_projection_records(); records.extend(suffix);
            for paged in [false, true] {
                let (result, events) = project_terminal_fixture(&records, paged, false, false);
                assert!(result.is_err());
                assert!(!events.iter().any(|event| event["type"] == "nokiy.terminal_evidence"));
            }
        }
        let mut records = terminal_projection_records(); records.push(terminal_marker().to_string());
        let (result, events) = project_terminal_fixture(&records, true, false, true);
        assert!(result.is_err(), "terminal snapshot drift must prevent marker emission");
        assert!(!events.iter().any(|event| event["type"] == "nokiy.terminal_evidence"));
        let cases = [
            vec![terminal_marker().to_string()],
            vec![terminal_projection_records()[0].clone(), terminal_marker().to_string()],
            {
                let mut records = terminal_projection_records();
                records.push(json!({"type":"runtime_provider_observation", "runtime_id":"newer-runtime"}).to_string());
                records.push(terminal_marker().to_string());
                records
            },
        ];
        for records in cases {
            for paged in [false, true] {
                let (result, events) = project_terminal_fixture(&records, paged, false, false);
                assert!(result.is_err(), "unbound or stale runtime must not become terminal evidence");
                assert!(!events.iter().any(|event| event["type"] == "nokiy.terminal_evidence"));
            }
        }
    }

    #[test]
    fn terminal_evidence_jsonl_ingress_requires_persisted_projection_and_deduplicates_output() {
        let marker = terminal_marker();
        assert!(super::emit_jsonl(&marker).is_err(), "direct/unpersisted event must not print");
        super::TERMINAL_EVIDENCE_WRITTEN.with(|written| *written.borrow_mut() = Some(marker.clone()));
        super::TERMINAL_EVIDENCE_READY.with(|ready| *ready.borrow_mut() = Some(marker.clone()));
        super::emit_jsonl(&marker).expect("already emitted validated event is not printed again");
        let mut conflicting = marker.clone(); conflicting["runtime_id"] = json!("different-runtime");
        super::TERMINAL_EVIDENCE_READY.with(|ready| *ready.borrow_mut() = Some(conflicting.clone()));
        assert!(super::emit_jsonl(&conflicting).is_err());
        super::TERMINAL_EVIDENCE_READY.with(|ready| *ready.borrow_mut() = None);
        super::TERMINAL_EVIDENCE_WRITTEN.with(|written| *written.borrow_mut() = None);
    }

    #[test]
    fn terminal_evidence_is_never_inferred_from_missing_assistant_or_done_tools() {
        let records: Vec<_> = terminal_projection_records().into_iter()
            .filter(|record| !record.contains("Already published assistant text.")).collect();
        for paged in [false, true] {
            let (result, events) = project_terminal_fixture(&records, paged, false, false);
            result.expect("ordinary history");
            assert!(!events.iter().any(|event| event["type"] == "nokiy.terminal_evidence"));
        }
    }

    #[test]
    fn provider_summary_requires_full_unanimity_and_deduplicates_runtime_ids() {
        let record = |id: &str, tier: &str, model: &str| {
            json!({
                "type": "runtime_provider_observation", "runtime_id": id,
                "provider_observation": {"schema_version":"provider_observation_v1",
                    "source":"provider_response", "response_id":format!("resp-{id}"),
                    "model":model, "service_tier":tier}
            })
            .to_string()
        };
        let priority = record("a", "priority", "gpt-6");
        let default = record("b", "default", "gpt-6");
        let one = aggregate_provider_observations(&[priority.clone(), priority.clone()]);
        assert_eq!(one["runtime_count"], 1);
        assert_eq!(one["service_tier"]["value"], "priority");
        assert_eq!(
            aggregate_provider_observations(&[default.clone()])["service_tier"]["value"],
            "default"
        );
        let mixed = aggregate_provider_observations(&[priority.clone(), default]);
        assert!(mixed["service_tier"]["value"].is_null());
        assert_eq!(
            mixed["service_tier"]["distinct_values"],
            json!(["default", "priority"])
        );
        assert_eq!(mixed["model"]["value"], "gpt-6");
        let partial = aggregate_provider_observations(&[
            priority.clone(),
            json!({"type":"runtime_usage", "runtime_id":"b", "usage":{"total_tokens":3}})
                .to_string(),
        ]);
        assert_eq!(partial["runtime_count"], 2);
        assert!(partial["service_tier"]["value"].is_null());
        let unobserved = aggregate_provider_observations(&[
            priority.clone(),
            json!({
                "type":"runtime_provider_observation", "runtime_id":"early-completion",
                "provider_observation":null
            })
            .to_string(),
        ]);
        assert_eq!(unobserved["runtime_count"], 2);
        assert_eq!(unobserved["conflict_count"], 0);
        assert!(unobserved["service_tier"]["value"].is_null());
        let conflicting =
            aggregate_provider_observations(&[priority, record("a", "default", "gpt-6")]);
        assert_eq!(conflicting["runtime_count"], 1);
        assert_eq!(conflicting["conflict_count"], 1);
        assert!(conflicting["service_tier"]["value"].is_null());
    }

    #[test]
    fn execution_projection_replays_early_command_and_file_evidence_across_pages_exactly_once() {
        use session_log_contract::{ExecutionEvidencePage, ExecutionEvidenceReference, ExecutionEvidenceSnapshot, SessionContextRecord};
        let command = |kind: &str, text: &str| json!({"type":"tool_result", "tool_name":"command_run",
            "input":{"commands":[]}, "output":{"results":[{"command_type":kind,
                "command_line":text, "success":true, "output":{"stdout":text},
                "changes":[{"path":"early.rs","kind":"update"}]}]}}).to_string();
        let records = vec![command("shell_command", "EARLY_COMMAND"),
            command("apply_patch", "EARLY_PATCH"),
            json!({"type":"context_compaction","content":"first checkpoint"}).to_string(),
            command("shell_command", "AFTER_FIRST_COMPACTION"),
            json!({"type":"context_compaction","content":"second checkpoint"}).to_string(),
            command("shell_command", "RETAINED_TAIL")];
        let reference = ExecutionEvidenceReference::new(ExecutionEvidenceSnapshot {
            session_id:"projection-history".to_string(), next_sequence:records.len() as u64,
            next_management_sequence:3, retained_from_sequence:5 }, 100);
        let mut events = Vec::new();
        let mut pages = 0;
        super::project_execution_evidence_with(&reference, &PathBuf::from("/workspace"), false, false,
            |request| {
                pages += 1;
                let end = (request.from_sequence + 2).min(reference.snapshot.next_sequence);
                Ok(ExecutionEvidencePage { snapshot:reference.snapshot.clone(), next_sequence:end,
                    records:(request.from_sequence..end).map(|sequence| SessionContextRecord {
                        sequence, raw_record:records[sequence as usize].clone() }).collect(), summary:None })
            }, |event| { events.push(event.clone()); Ok(()) }).expect("complete historical projection");
        assert_eq!(pages, 4); // three short pages and the terminal identity check
        assert_eq!(events.len(), 4);
        assert!(events[0]["item"]["command"].as_str().expect("command").contains("EARLY_COMMAND"));
        assert_eq!(events[1]["item"]["type"], "file_change");
        assert_eq!(events[1]["item"]["changes"][0]["path"], "/workspace/early.rs");
        assert_eq!(events[0]["execution_evidence"]["sequence"], 0);
        assert_eq!(events[1]["execution_evidence"]["sequence"], 1);
        assert_eq!(events[2]["execution_evidence"]["sequence"], 3);
        assert_eq!(events[3]["execution_evidence"]["sequence"], 5);
        let ids = events.iter().map(|event| event["item"]["id"].as_str().expect("stable item id")).collect::<std::collections::BTreeSet<_>>();
        assert_eq!(ids.len(), events.len());
        assert!(events.iter().all(|event| event["execution_evidence"]["session_id"] == "projection-history"));

        let mut traversed = 0;
        super::project_execution_evidence_with(&reference, &PathBuf::from("/workspace"), false, true,
            |request| {
                let end = (request.from_sequence + 2).min(reference.snapshot.next_sequence);
                traversed += end - request.from_sequence;
                Ok(ExecutionEvidencePage { snapshot:reference.snapshot.clone(), next_sequence:end,
                    records:(request.from_sequence..end).map(|sequence| SessionContextRecord {
                        sequence, raw_record:records[sequence as usize].clone() }).collect(), summary:None })
            }, |_| panic!("live command/file evidence must not be emitted twice")).expect("live evidence still verifies all pages");
        assert_eq!(traversed, reference.snapshot.next_sequence);
    }

    #[test]
    fn execution_projection_rejects_missing_drifted_and_mixed_evidence_without_tail_fallback() {
        use session_log_contract::{ExecutionEvidencePage, ExecutionEvidenceReference, ExecutionEvidenceSnapshot};
        let reference = ExecutionEvidenceReference::new(ExecutionEvidenceSnapshot { session_id:"expected".to_string(),
            next_sequence:10, next_management_sequence:3, retained_from_sequence:9 }, 100);
        assert!(super::project_execution_evidence_with(&reference, &PathBuf::from("/workspace"), false, false,
            |_| Err("missing history".to_string()), |_| panic!("must not project a tail")).is_err());
        assert!(super::project_execution_evidence_with(&reference, &PathBuf::from("/workspace"), false, false,
            |_| { let mut snapshot = reference.snapshot.clone(); snapshot.next_management_sequence += 1;
                Ok(ExecutionEvidencePage { snapshot, next_sequence:0, records:Vec::new(), summary:None }) },
            |_| panic!("must not project drifted evidence")).is_err());
        let encoded = serde_json::to_string(&reference).expect("reference");
        assert!(super::execution_evidence_reference(&[encoded.clone(), json!({"role":"assistant","content":"tail"}).to_string()]).is_err());
        let bad = json!({"type":"execution_evidence_v1", "snapshot":{"session_id":"expected"}}).to_string();
        assert!(aggregate_runtime_usage(&[bad]).get("_execution_evidence_error").is_some());
        let terminal = json!({"type":"turn.completed", "session_id":"other", "cwd":"/workspace",
            "usage":{"_execution_evidence":reference}, "provider_observation":{}});
        assert!(super::emit_jsonl(&terminal).is_err(), "wrong identity must fail before any terminal output");
    }

    #[test]
    fn aggregate_runtime_usage_sums_known_fields_and_derives_total_when_missing() {
        let log = vec![
            json!({
                "type": "runtime_usage",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 3,
                    "cache_write_tokens": 2,
                    "output_tokens": 5,
                    "reasoning_tokens": 7,
                    "latency_ms": 100
                }
            })
            .to_string(),
            json!({
                "type": "runtime_usage",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "reasoning_tokens": 3,
                    "total_tokens": 99,
                    "latency_ms": 4
                }
            })
            .to_string(),
            "not json".to_string(),
        ];

        let usage = aggregate_runtime_usage(&log);

        assert_eq!(usage["input_tokens"], 11);
        assert_eq!(usage["cached_input_tokens"], 3);
        assert_eq!(usage["cache_write_tokens"], 2);
        assert_eq!(usage["output_tokens"], 7);
        assert_eq!(usage["reasoning_tokens"], 10);
        assert_eq!(usage["reasoning_output_tokens"], 10);
        assert_eq!(usage["total_tokens"], 99);
        assert_eq!(usage["latency_ms"], 104);
        assert_eq!(usage["coverage"]["status"], "unknown");

        let derived = aggregate_runtime_usage(&[json!({
            "type": "runtime_usage",
            "usage": {"input_tokens": 2, "output_tokens": 3, "reasoning_tokens": 4}
        })
        .to_string()]);
        assert_eq!(derived["total_tokens"], 9);
        assert_eq!(derived["coverage"]["status"], "unknown");
    }

    #[test]
    fn aggregate_runtime_usage_coverage_complete_and_turn_completed_forwards_it() {
        let log = ["a", "b"]
            .into_iter()
            .flat_map(|id| {
                [
                    json!({"type":"runtime_provider_observation", "runtime_id":id}).to_string(),
                    json!({"type":"runtime_usage", "runtime_id":id,
                    "usage":{"input_tokens":2, "output_tokens":3, "total_tokens":5}})
                    .to_string(),
                ]
            })
            .collect::<Vec<_>>();
        let usage = aggregate_runtime_usage(&log);
        assert_eq!(
            usage["coverage"],
            json!({
                "schema_version":"runtime_usage_coverage_v1",
                "scope":"recorded_session_context_runtimes_not_provider_attempts_or_billing",
                "known_runtime_count":2, "valid_usage_count":2,
                "missing_usage_count":0, "status":"complete"
            })
        );
        assert_eq!(usage["total_tokens"], 10);
        let config =
            CliConfig::parse(vec!["exec".to_string(), "inspect".to_string()]).expect("parse cli");
        let event = turn_completed_event(
            &config,
            "session",
            usage.clone(),
            aggregate_provider_observations(&log),
            "completed",
            None,
        );
        assert_eq!(event["usage"]["coverage"], usage["coverage"]);
        assert_eq!(event["status"], "completed");
    }

    #[test]
    fn aggregate_runtime_usage_coverage_detects_seventeen_sixteen_gap() {
        let mut log = (0..17)
            .map(|id| {
                json!({
                    "type":"runtime_provider_observation", "runtime_id":format!("runtime-{id}")
                })
                .to_string()
            })
            .collect::<Vec<_>>();
        log.extend((0..16).map(|id| {
            json!({
                "type":"runtime_usage", "runtime_id":format!("runtime-{id}"),
                "usage":{"input_tokens":1, "output_tokens":1, "total_tokens":2}
            })
            .to_string()
        }));
        let usage = aggregate_runtime_usage(&log);
        assert_eq!(usage["coverage"]["known_runtime_count"], 17);
        assert_eq!(usage["coverage"]["valid_usage_count"], 16);
        assert_eq!(usage["coverage"]["missing_usage_count"], 1);
        assert_eq!(usage["coverage"]["status"], "incomplete");
        assert_eq!(usage["total_tokens"], 32);
    }

    #[test]
    fn aggregate_runtime_usage_coverage_rejects_duplicate_and_conflicting_usage() {
        let record = |total| {
            json!({"type":"runtime_usage", "runtime_id":"a",
            "usage":{"input_tokens":1, "output_tokens":2, "total_tokens":total}})
            .to_string()
        };
        for log in [[record(3), record(3)], [record(3), record(4)]] {
            let usage = aggregate_runtime_usage(&log);
            assert_eq!(usage["coverage"]["known_runtime_count"], 1);
            assert_eq!(usage["coverage"]["valid_usage_count"], 0);
            assert_eq!(usage["coverage"]["missing_usage_count"], 1);
            assert_eq!(usage["coverage"]["status"], "incomplete");
            assert_eq!(usage["input_tokens"], 2);
        }
    }

    #[test]
    fn aggregate_runtime_usage_coverage_rejects_partial_invalid_and_anonymous_records() {
        let valid = json!({"type":"runtime_usage", "runtime_id":"a",
            "usage":{"input_tokens":1, "output_tokens":2, "total_tokens":3}})
        .to_string();
        let partial = json!({"type":"runtime_usage", "runtime_id":"b",
            "usage":{"input_tokens":1, "output_tokens":2}})
        .to_string();
        let invalid = json!({"type":"runtime_usage", "runtime_id":"c",
            "usage":{"input_tokens":1, "output_tokens":2, "total_tokens":3,
                     "latency_ms":-1}})
        .to_string();
        let anonymous = json!({"type":"runtime_usage",
            "usage":{"input_tokens":1, "output_tokens":2, "total_tokens":3}})
        .to_string();
        let log = [
            valid.clone(),
            partial,
            invalid,
            json!({"type":"runtime_provider_observation", "runtime_id":"d"}).to_string(),
            anonymous,
        ];
        let coverage = &aggregate_runtime_usage(&log)["coverage"];
        assert_eq!(coverage["known_runtime_count"], 4);
        assert_eq!(coverage["valid_usage_count"], 1);
        assert_eq!(coverage["missing_usage_count"], 3);
        assert_eq!(coverage["status"], "incomplete");
        for extra in [
            json!({"type":"runtime_usage", "runtime_id":" ",
            "usage":{"input_tokens":1, "output_tokens":2, "total_tokens":3}})
            .to_string(),
            json!({"type":"runtime_usage", "runtime_id":"a", "usage":null}).to_string(),
            json!({"type":"runtime_usage", "runtime_id":"a",
                "usage":{"input_tokens":"1", "output_tokens":2, "total_tokens":3}})
            .to_string(),
            json!({"type":"runtime_usage", "runtime_id":"a",
                "usage":{"input_tokens":1, "output_tokens":2, "total_tokens":3,
                         "reasoning_tokens":1.5}})
            .to_string(),
        ] {
            let coverage = &aggregate_runtime_usage(&[valid.clone(), extra])["coverage"];
            assert_eq!(coverage["status"], "incomplete");
        }
    }

    #[test]
    fn aggregate_runtime_usage_coverage_empty_and_legacy_are_unknown() {
        let empty = aggregate_runtime_usage(&[]);
        assert_eq!(empty["coverage"]["known_runtime_count"], 0);
        assert_eq!(empty["coverage"]["status"], "unknown");
        let legacy = aggregate_runtime_usage(&[json!({"type":"runtime_usage",
            "usage":{"input_tokens":2,"output_tokens":3,"total_tokens":5}})
        .to_string()]);
        assert_eq!(legacy["coverage"]["status"], "unknown");
        assert_eq!(legacy["total_tokens"], 5);
        let malformed = aggregate_runtime_usage(&[
            "not json".to_string(),
            json!({"type":"runtime_usage", "runtime_id":"a",
                "usage":{"input_tokens":2,"output_tokens":3,"total_tokens":5}})
            .to_string(),
        ]);
        assert_eq!(malformed["coverage"]["status"], "incomplete");
    }

    #[test]
    fn cli_run_events_include_benchmark_metadata() {
        let config = CliConfig::parse(vec![
            "exec".to_string(),
            "--cwd".to_string(),
            "C:/workspace".to_string(),
            "--agent-id".to_string(),
            "tura-direct".to_string(),
            "--model".to_string(),
            "gpt-5.5".to_string(),
            "--model-reasoning-effort".to_string(),
            "medium".to_string(),
            "--priority".to_string(),
            "hi".to_string(),
        ])
        .expect("parse cli");

        let started = thread_started_event(&config, "session-1");
        let completed = turn_completed_event(
            &config,
            "session-1",
            json!({"total_tokens": 42}),
            aggregate_provider_observations(&[]),
            "completed",
            None,
        );

        assert_eq!(started["type"], "thread.started");
        assert_eq!(started["thread_id"], "session-1");
        assert_eq!(started["session_id"], "session-1");
        assert_eq!(started["agent"], "tura-direct");
        assert_eq!(started["model"], "openai/gpt-5.5");
        assert_eq!(started["reasoning_effort"], "medium");
        assert_eq!(started["service_tier"], "priority");
        assert_eq!(started["metadata"]["agent"], "tura-direct");
        assert_eq!(completed["type"], "turn.completed");
        assert_eq!(completed["status"], "completed");
        assert_eq!(completed["usage"]["total_tokens"], 42);
        assert_eq!(completed["metadata"]["model"], "openai/gpt-5.5");
    }

    #[test]
    fn cli_run_events_report_ultrafast_without_mislabeling_it_as_priority() {
        let config = CliConfig::parse(vec![
            "exec".to_string(),
            "--service-tier".to_string(),
            "ultrafast".to_string(),
            "inspect".to_string(),
        ])
        .expect("parse ultrafast cli");

        let started = thread_started_event(&config, "session-ultrafast");

        assert_eq!(started["service_tier"], "ultrafast");
        assert_eq!(started["priority"], false);
        assert_eq!(started["acceleration_enabled"], true);
        assert_eq!(started["metadata"]["service_tier"], "ultrafast");
    }

    #[test]
    fn clean_agent_message_removes_raw_tool_payloads_and_keeps_visible_prefix() {
        assert_eq!(clean_agent_message("  hello user  "), "hello user");
        assert_eq!(
            clean_agent_message(r#"{"commands":[{"command":"pwd"}]}"#),
            ""
        );
        assert_eq!(
            clean_agent_message(r#"Done. {"commands":[{"command":"pwd"}]}"#),
            "Done."
        );
        assert_eq!(clean_agent_message(r#"{"reply_message":"hidden"}"#), "");
        assert_eq!(clean_agent_message(""), "");
    }

    #[test]
    fn turn_log_summary_reports_only_current_turn_usage_timing_tools_and_text() {
        let log = vec![
            json!({"role": "user", "content": "old", "created_at": 1000}).to_string(),
            json!({"type": "runtime_usage", "usage": {"input_tokens": 100, "output_tokens": 1, "total_tokens": 101, "latency_ms": 9}, "timestamp": "2026-01-01T00:00:01Z"}).to_string(),
            json!({"role": "assistant", "content": "old answer", "created_at": 1100}).to_string(),
            json!({"role": "user", "content": [{"type": "input_text", "text": "new"}], "created_at": 2000}).to_string(),
            json!({"type": "tool_result", "tool_name": "command_run", "input": {"commands": [{"command_type": "shell_command", "command_line": "echo ok"}]}, "output": {"results": [{"success": true}]}, "success": true, "timestamp": "2026-01-01T00:00:03Z"}).to_string(),
            json!({"type": "runtime_usage", "usage": {"input_tokens": 10, "output_tokens": 5, "reasoning_tokens": 2, "total_tokens": 17, "latency_ms": 250}, "timestamp": "2026-01-01T00:00:04Z"}).to_string(),
            json!({"role": "assistant", "content": " final ", "created_at": 5000, "runtime_id": "runtime-new"}).to_string(),
        ];

        let summary = turn_log_summary(&log, None);

        assert_eq!(summary["usage"]["input_tokens"], 10);
        assert_eq!(summary["usage"]["total_tokens"], 17);
        assert_eq!(summary["timing"]["provider_latency_ms"], 250);
        assert_eq!(summary["tool_calls"].as_array().expect("tools").len(), 1);
        assert_eq!(summary["tool_calls"][0]["tool_name"], "command_run");
        let text = summary["text"].as_array().expect("text");
        assert_eq!(text.len(), 2);
        assert_eq!(text[0]["role"], "user");
        assert_eq!(text[0]["content"], "new");
        assert_eq!(text[1]["role"], "assistant");
        assert_eq!(text[1]["content"], "final");
    }

    #[test]
    fn flatten_command_results_merges_input_command_metadata_and_batch_children() {
        let output = json!({
            "results": [
                {
                    "results": [
                        {"success": true, "stdout": "nested"}
                    ]
                },
                {
                    "success": false,
                    "output": "plain"
                }
            ]
        });
        let input = json!({
            "commands": [
                {"command_type": "shell_command", "command_line": "echo nested"},
                {"command": "apply_patch", "command_line": "*** Begin Patch"}
            ]
        });

        let flattened = flatten_command_results(&output, &input);

        assert_eq!(flattened.len(), 2);
        assert_eq!(flattened[0]["stdout"], "nested");
        assert_eq!(flattened[1]["command_type"], "apply_patch");
        assert_eq!(flattened[1]["command_line"], "*** Begin Patch");
        assert_eq!(
            flatten_command_results(&json!({"ok": true}), &Value::Null),
            vec![json!({"ok": true})]
        );

        let streamed = flatten_command_results(
            &json!({
                "streamed_command_run_result": {
                    "results": [
                        {"command_type": "shell_command", "output": "ok"}
                    ]
                }
            }),
            &Value::Null,
        );
        assert_eq!(
            streamed,
            vec![json!({"command_type": "shell_command", "output": "ok"})]
        );
    }

    #[test]
    fn file_changes_prefers_explicit_changes_and_falls_back_to_workspace() {
        let cwd = PathBuf::from("C:/workspace");
        let changes = file_changes(
            &json!({
                "response": {
                    "changes": [
                        {"path": "src/lib.rs", "kind": "update"},
                        {"path": "C:/abs/file.rs", "kind": "create"},
                        {"kind": "missing-path"}
                    ]
                }
            }),
            &cwd,
        );

        assert_eq!(changes.len(), 2);
        assert!(
            changes[0]["path"]
                .as_str()
                .unwrap_or_default()
                .replace('\\', "/")
                .ends_with("C:/workspace/src/lib.rs")
        );
        assert_eq!(changes[0]["kind"], "update");
        assert_eq!(changes[1]["kind"], "create");

        let fallback = file_changes(&json!({}), &cwd);
        assert_eq!(
            fallback,
            vec![json!({"path": cwd.to_string_lossy().to_string(), "kind": "update"})]
        );
    }

    #[test]
    fn display_and_output_helpers_render_shell_and_structured_outputs() {
        let shell = json!({
            "command_type": "shell_command",
            "command_line": "echo 'hello'",
            "output": "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nhello\n"
        });
        let non_shell = json!({
            "command_type": "read_media",
            "display_command": "read_media photo.png",
            "output": {"summary": "ok"}
        });

        let display = display_command(&shell);
        assert!(display.contains("echo ''hello''") || display.contains("echo 'hello'"));
        assert_eq!(display_command(&non_shell), "read_media photo.png");
        assert_eq!(command_output(&shell), "hello\n");
        assert_eq!(command_output(&json!({"stdout": "direct"})), "direct");
        assert_eq!(command_output(&non_shell), "{\"summary\":\"ok\"}");
        assert_eq!(
            shell_display_output("Exit code: 0\nWall time: 0.1 seconds\nOutput:\nbody"),
            "body"
        );
        assert_eq!(shell_display_output("plain output"), "plain output");
    }

    #[test]
    fn command_output_preserves_preexecution_error_without_inventing_output() {
        let error = "JSPACE_SOURCE_READ_INVALID: operation=read target=source_read detail=SOURCE_READ_ARGUMENTS_INVALID: duplicate field `path` at line 1 column 68";
        let failed = json!({
            "success": false, "command_type": "jspace", "error": error,
            "effect_state": "not_started"
        });
        assert_eq!(command_output(&failed), error);
        assert_eq!(
            command_output(&json!({"stdout": "", "output": "", "error": error})),
            error
        );
        assert_eq!(
            command_output(&json!({"stdout": null, "output": null, "error": error})),
            error
        );
        assert_eq!(
            command_output(&json!({"output": "null", "error": error})),
            "null"
        );

        let receipt = json!({"path": "src/lib.rs", "source_sha256": "abc"});
        assert_eq!(
            command_output(&json!({"stdout": "source text", "output": receipt, "error": error})),
            "source text"
        );
        assert_eq!(
            command_output(&json!({"stdout": "", "output": receipt, "error": error})),
            receipt.to_string()
        );
        assert_eq!(
            command_output(&json!({"stdout": "", "output": "receipt text", "error": error})),
            "receipt text"
        );
        assert_eq!(
            command_output(&json!({"stdout": "direct", "output": "other", "error": error})),
            "direct"
        );
        assert_eq!(
            command_output(
                &json!({"output": "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nbody", "error": error})
            ),
            "body"
        );
        assert_eq!(command_output(&json!({"stdout": "", "output": null})), "");
        assert_eq!(
            command_output(&json!({"output": null, "error": "  "})),
            "null"
        );
        assert_eq!(
            command_output(&json!({"stdout": "  ", "output": "other"})),
            "  "
        );
        assert_eq!(
            command_output(&json!({"stdout": "", "output": receipt})),
            ""
        );
        assert_eq!(
            command_output(&json!({"stderr": "real stderr"})),
            "real stderr"
        );
    }
}
