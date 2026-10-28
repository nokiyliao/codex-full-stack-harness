use crate::CommandCheckpoint;
use lifecycle::{
    ContextTokenStats, RuntimeAggregate, RuntimeEvent, RuntimeProjection, RuntimeState,
    SessionCommand, SessionEvent, SessionManagement, SessionManagementDelta, SessionProjection,
    SessionState, UsageReport,
};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub struct Page {
    #[serde(default)]
    pub page: u64,
    #[serde(default = "default_page_size")]
    pub page_size: u64,
    pub total: u64,
}

fn default_page_size() -> u64 {
    50
}

impl Default for Page {
    fn default() -> Self {
        Self {
            page: 0,
            page_size: default_page_size(),
            total: 0,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkspaceSummary {
    pub directory: String,
    pub session_count: u64,
    pub last_updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SessionMetadata {
    pub session_directory: String,
    pub model: Option<String>,
    pub agent: Option<String>,
    pub session_type: String,
    pub kill_processes_on_start: bool,
    pub validator_enabled: bool,
    pub force_planning: bool,
    pub model_variant: Option<String>,
    pub model_acceleration_enabled: bool,
    pub disable_permission_restrictions: bool,
    pub use_last_tool_call_response: bool,
    pub auto_session_name: bool,
    pub context_tokens: ContextTokenStats,
    pub runtime_usage: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SessionSnapshot {
    pub session_id: String,
    pub workspace: String,
    pub name: Option<String>,
    pub created_at: i64,
    pub updated_at: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_user_message_at: Option<i64>,
    pub message_count: u64,
    pub lifecycle_projection: SessionProjection,
    pub management: SessionManagement,
    pub metadata: SessionMetadata,
    #[serde(default)]
    pub todos: Vec<serde_json::Value>,
}

impl SessionSnapshot {
    pub fn validate(&self) -> Result<(), String> {
        if self.session_id != self.lifecycle_projection.session_id {
            return Err(format!(
                "snapshot session id {} does not match lifecycle projection {}",
                self.session_id, self.lifecycle_projection.session_id
            ));
        }
        if self.session_id != self.management.session_id {
            return Err(format!(
                "snapshot session id {} does not match management {}",
                self.session_id, self.management.session_id
            ));
        }
        let management_projection = self.management.lifecycle_projection();
        if management_projection.state != self.lifecycle_projection.state
            || management_projection.task_plan != self.lifecycle_projection.task_plan
        {
            return Err(format!(
                "snapshot lifecycle projection does not match persisted management for {}",
                self.session_id
            ));
        }
        if self.name.as_deref() != Some(self.management.session_name.as_str()) {
            return Err(format!(
                "snapshot name does not match persisted management for {}",
                self.session_id
            ));
        }
        let management_directory = self.management.session_directory.to_string_lossy();
        if self.metadata.session_directory != management_directory {
            return Err(format!(
                "snapshot metadata directory does not match persisted management for {}",
                self.session_id
            ));
        }
        if self.metadata.disable_permission_restrictions
            != self.management.disable_permission_restrictions
            || self.metadata.use_last_tool_call_response
                != self.management.use_last_tool_call_response
            || self.metadata.auto_session_name != self.management.auto_session_name
            || self.metadata.context_tokens != self.management.context_tokens
            || self.metadata.runtime_usage != self.management.runtime_usage
        {
            return Err(format!(
                "snapshot metadata does not match persisted management for {}",
                self.session_id
            ));
        }
        Ok(())
    }

    pub fn into_management(mut self) -> Result<SessionManagement, String> {
        self.validate()?;
        self.management
            .replace_lifecycle_projection(self.lifecycle_projection);
        Ok(self.management)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionSummary {
    pub session_id: String,
    pub workspace: String,
    pub name: Option<String>,
    pub parent_id: Option<String>,
    pub created_at: i64,
    pub updated_at: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_user_message_at: Option<i64>,
    pub state: Option<String>,
    pub status: Option<String>,
    pub message_count: u64,
    #[serde(default)]
    pub feed_cursor: u64,
    pub task_management: serde_json::Value,
    pub metadata: SessionMetadata,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionRecord {
    pub session_id: String,
    pub message_id: String,
    pub role: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub record: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SessionContextRecord {
    pub sequence: u64,
    pub raw_record: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SessionRecordProjection {
    pub session_id: String,
    pub message_id: String,
    pub role: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub record: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SessionDeltaEntry {
    pub context: SessionContextRecord,
    #[serde(deserialize_with = "Option::deserialize")]
    pub projection: Option<SessionRecordProjection>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PersistSessionDeltaRequest {
    pub session_id: String,
    pub management_sequence: u64,
    pub management_delta: SessionManagementDelta,
    pub retained_from_sequence: u64,
    pub entries: Vec<SessionDeltaEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReadContextSliceRequest {
    pub session_id: String,
    pub max_estimated_tokens: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ContextSlice {
    pub records: Vec<SessionContextRecord>,
    pub retained_from_sequence: u64,
    pub next_sequence: u64,
    pub next_management_sequence: u64,
}

// Execution output reads the immutable raw history, never the retained prompt window.
pub const EXECUTION_EVIDENCE_PAGE_RECORDS: u64 = 128;
pub const EXECUTION_EVIDENCE_PAGE_BYTES: u64 = 4 * 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ExecutionEvidenceSnapshot {
    pub session_id: String,
    pub next_sequence: u64,
    pub next_management_sequence: u64,
    pub retained_from_sequence: u64,
}

impl ExecutionEvidenceSnapshot {
    pub fn validate(&self) -> Result<(), String> {
        if self.session_id.trim().is_empty() || self.retained_from_sequence > self.next_sequence {
            return Err("invalid execution evidence identity or retention cursor".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReadExecutionEvidenceRequest {
    pub session_id: String,
    pub snapshot: Option<ExecutionEvidenceSnapshot>,
    pub from_sequence: u64,
    pub max_records: u64,
    pub max_bytes: u64,
    pub include_summary: bool,
}

impl ReadExecutionEvidenceRequest {
    pub fn validate(&self) -> Result<(), String> {
        if self.session_id.trim().is_empty()
            || !(1..=EXECUTION_EVIDENCE_PAGE_RECORDS).contains(&self.max_records)
            || !(1..=EXECUTION_EVIDENCE_PAGE_BYTES).contains(&self.max_bytes)
        {
            return Err("invalid execution evidence identity or page bounds".to_string());
        }
        if let Some(snapshot) = &self.snapshot {
            snapshot.validate()?;
            if snapshot.session_id != self.session_id || self.from_sequence > snapshot.next_sequence {
                return Err("execution evidence request identity/cursor mismatch".to_string());
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ExecutionEvidenceSummary {
    pub usage: serde_json::Value,
    pub provider_observation: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ExecutionEvidencePage {
    pub snapshot: ExecutionEvidenceSnapshot,
    pub next_sequence: u64,
    pub records: Vec<SessionContextRecord>,
    pub summary: Option<ExecutionEvidenceSummary>,
}

impl ExecutionEvidencePage {
    pub fn validate(&self, request: &ReadExecutionEvidenceRequest) -> Result<(), String> {
        request.validate()?;
        self.snapshot.validate()?;
        if self.snapshot.session_id != request.session_id
            || request.snapshot.as_ref().is_some_and(|expected| expected != &self.snapshot)
            || request.from_sequence > self.snapshot.next_sequence
            || self.records.len() as u64 > request.max_records
            || self.summary.is_some() != request.include_summary
        {
            return Err("execution evidence snapshot/identity drift".to_string());
        }
        let mut next = request.from_sequence;
        let mut bytes = 0_u64;
        for record in &self.records {
            if record.sequence != next || next >= self.snapshot.next_sequence {
                return Err(format!("execution evidence sequence gap/duplicate at {next}"));
            }
            bytes = bytes.checked_add(record.raw_record.len() as u64)
                .ok_or("execution evidence page byte overflow")?;
            if bytes > request.max_bytes {
                return Err("execution evidence page exceeds byte bound".to_string());
            }
            let value: serde_json::Value = serde_json::from_str(&record.raw_record)
                .map_err(|error| format!("invalid execution evidence record {next}: {error}"))?;
            if !value.is_object()
                || value.get("session_id").is_some_and(|id| id.as_str() != Some(request.session_id.as_str()))
            {
                return Err(format!("execution evidence record {next} has wrong session identity"));
            }
            next += 1;
        }
        if self.next_sequence != next || (next < self.snapshot.next_sequence && self.records.is_empty()) {
            return Err("execution evidence traversal made no progress or changed cursor".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ExecutionEvidenceReference {
    #[serde(rename = "type")]
    pub record_type: String,
    pub snapshot: ExecutionEvidenceSnapshot,
    pub turn_started_at_ms: i64,
}

impl ExecutionEvidenceReference {
    pub fn new(snapshot: ExecutionEvidenceSnapshot, turn_started_at_ms: i64) -> Self {
        Self { record_type: "execution_evidence_v1".to_string(), snapshot, turn_started_at_ms }
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.record_type != "execution_evidence_v1" {
            return Err("unsupported execution evidence reference".to_string());
        }
        self.snapshot.validate()
    }
}

/// One page is dropped before requesting the next; even the empty terminal page
/// is checked, so a changing session cannot produce a complete-looking prefix.
pub fn visit_execution_evidence(
    snapshot: &ExecutionEvidenceSnapshot,
    mut read: impl FnMut(ReadExecutionEvidenceRequest) -> Result<ExecutionEvidencePage, String>,
    mut visit: impl FnMut(&SessionContextRecord) -> Result<(), String>,
) -> Result<(), String> {
    snapshot.validate()?;
    let mut from_sequence = 0;
    loop {
        let request = ReadExecutionEvidenceRequest {
            session_id: snapshot.session_id.clone(), snapshot: Some(snapshot.clone()),
            from_sequence, max_records: EXECUTION_EVIDENCE_PAGE_RECORDS,
            max_bytes: EXECUTION_EVIDENCE_PAGE_BYTES, include_summary: false,
        };
        let page = read(request.clone())?;
        page.validate(&request)?;
        for record in &page.records { visit(record)?; }
        if from_sequence == snapshot.next_sequence { return Ok(()); }
        from_sequence = page.next_sequence;
    }
}

pub fn observed_evidence_field(value: &serde_json::Value, key: &str) -> Option<String> {
    value.get(key).and_then(serde_json::Value::as_str).map(str::trim)
        .filter(|text| !text.is_empty() && text.len() <= 256 && !text.chars().any(char::is_control))
        .map(str::to_owned)
}

pub const EVIDENCE_USAGE_KEYS: [&str; 7] = [
    "input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens",
    "reasoning_tokens", "total_tokens", "latency_ms",
];

/// Shared parser for one runtime. Canonical reads order/group on disk instead
/// of keeping an unbounded map of every historical runtime in the caller.
#[derive(Debug, Default)]
pub struct RuntimeEvidenceState {
    pub usage_records: u64,
    pub usage_valid: bool,
    pub tokens: [u64; 7],
    pub observation: Option<(Option<String>, Option<String>, Option<String>)>,
    pub observation_conflict: bool,
    provider_observation_records: u64,
    provider_response_seen: bool,
    pre_model_marker_seen: bool,
    pre_model_marker_valid: bool,
    failed_attempts: Option<serde_json::Value>,
    failed_attempts_conflict: bool,
}

impl RuntimeEvidenceState {
    fn verified_pre_model_failure(&self) -> bool {
        self.provider_observation_records == 1 && self.pre_model_marker_valid
            && self.usage_records == 0 && !self.provider_response_seen
    }

    pub fn add(&mut self, value: &serde_json::Value) {
        if value.get("type").and_then(serde_json::Value::as_str) == Some("runtime_usage") {
            let usage = &value["usage"];
            let valid = ["input_tokens", "output_tokens", "total_tokens"].iter()
                .all(|key| usage.get(*key).and_then(serde_json::Value::as_u64).is_some())
                && ["cached_input_tokens", "cache_write_tokens", "reasoning_tokens", "latency_ms"].iter()
                    .all(|key| usage.get(*key).is_none_or(|field| field.as_u64().is_some()));
            let valid = valid && match (
                usage.get("input_tokens").and_then(serde_json::Value::as_u64),
                usage.get("output_tokens").and_then(serde_json::Value::as_u64),
                usage.get("total_tokens").and_then(serde_json::Value::as_u64),
            ) {
                (Some(input), Some(output), Some(total)) =>
                    input.checked_add(output).is_some_and(|minimum| total >= minimum),
                _ => false,
            };
            self.usage_valid = self.usage_records == 0 && valid;
            self.usage_records = self.usage_records.saturating_add(1);
            for (total, key) in self.tokens.iter_mut().zip(EVIDENCE_USAGE_KEYS) {
                *total = total.saturating_add(usage.get(key).and_then(serde_json::Value::as_u64).unwrap_or(0));
            }
        } else if value.get("type").and_then(serde_json::Value::as_str) == Some("runtime_provider_observation") {
            let observation = &value["provider_observation"];
            if let Some(attempts) = observation.get("failed_attempts") {
                if !attempts.is_array() || self.failed_attempts.as_ref().is_some_and(|old| old != attempts) {
                    self.failed_attempts_conflict = true;
                } else {
                    self.failed_attempts = Some(attempts.clone());
                }
            }
            self.provider_observation_records = self.provider_observation_records.saturating_add(1);
            self.provider_response_seen |= !observation.is_null();
            if let Some(marker) = value.get("provider_call_observation").filter(|marker| !marker.is_null()) {
                self.pre_model_marker_seen = true;
                self.pre_model_marker_valid |= observed_evidence_field(value, "runtime_id").is_some()
                    && observed_evidence_field(value, "session_id").is_some()
                    && value.get("status").and_then(serde_json::Value::as_str) == Some("Failed")
                    && value.get("error_code").and_then(serde_json::Value::as_str) == Some("CALL_FAILED")
                    && marker == &serde_json::json!({"schema_version":"provider_call_observation_v1",
                        "scope":"runtime_provider_call", "stage":"authentication", "model_provider_invoked":false});
            }
            let current = if observation.get("schema_version").and_then(serde_json::Value::as_str) == Some("provider_observation_v1")
                && observation.get("source").and_then(serde_json::Value::as_str) == Some("provider_response") {
                Some((observed_evidence_field(observation, "response_id"),
                    observed_evidence_field(observation, "model"), observed_evidence_field(observation, "service_tier")))
            } else { None };
            if self.observation.is_some() || self.observation_conflict {
                if self.observation != current {
                    self.observation_conflict = true;
                    self.observation = None;
                }
            } else if current.is_some() {
                self.observation = current;
            } else if !(observation.is_null()
                || (observation["schema_version"] == "provider_observation_v1"
                    && observation["source"] == "provider_failure"))
            {
                self.observation_conflict = true;
            }
        }
    }
}

#[derive(Debug, Default)]
pub struct RuntimeEvidenceTotals {
    pub tokens: [u64; 7],
    pub runtime_count: u64,
    pub valid_usage_count: u64,
    pub observation_count: u64,
    pub conflict_count: u64,
    pub unbound_or_malformed: bool,
    verified_pre_model_failure_count: u64,
    model_usage_conflict: bool,
    failed_usage: FailedAttemptUsage,
}

#[derive(Debug, Default)]
struct FailedAttemptUsage {
    attempts: u64,
    reported: u64,
    unknown: u64,
    invalid: u64,
    conflicts: u64,
    totals: std::collections::BTreeMap<String, u64>,
}

impl FailedAttemptUsage {
    fn add(&mut self, bound: bool, state: &RuntimeEvidenceState) {
        if state.failed_attempts_conflict || (!bound && state.failed_attempts.is_some()) {
            self.conflicts += 1;
            return;
        }
        let Some(attempts) = state.failed_attempts.as_ref().and_then(serde_json::Value::as_array) else { return; };
        for attempt in attempts {
            self.attempts += 1;
            let usage = &attempt["usage"];
            if usage.is_null() { self.unknown += 1; continue; }
            let fields = ["input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens"];
            let valid = usage.is_object() && usage["invalid_fields"] == serde_json::json!([])
                && fields.iter().all(|k| usage.get(*k).is_none_or(|v| v.is_null() || v.as_u64().is_some()));
            let count = |k: &str| usage.get(k).and_then(serde_json::Value::as_u64);
            let consistent = match (count("input_tokens"), count("output_tokens"), count("total_tokens")) {
                (Some(i), Some(o), Some(t)) => i.checked_add(o) == Some(t),
                _ => true,
            };
            let parts_valid = ![("cached_input_tokens", "input_tokens"), ("reasoning_tokens", "output_tokens")]
                .iter().any(|(a,b)| matches!((count(a),count(b)), (Some(a),Some(b)) if a>b));
            if !valid || !consistent || !parts_valid { self.invalid += 1; continue; }
            let values: Vec<_> = fields.iter().filter_map(|k| count(k).map(|v| (*k,v))).collect();
            if values.is_empty() { self.unknown += 1; continue; }
            if values.iter().any(|(k,v)| self.totals.get(*k).unwrap_or(&0).checked_add(*v).is_none()) {
                self.conflicts += 1;
                continue;
            }
            self.reported += 1;
            for (key,value) in values { *self.totals.entry(key.into()).or_default() += value; }
        }
    }

    fn summary(&self) -> serde_json::Value {
        serde_json::json!({"coverage":"partial", "scope":"reported_failed_attempts_only",
            "attempt_count":self.attempts, "reported_attempt_count":self.reported,
            "unknown_attempt_count":self.unknown, "invalid_attempt_count":self.invalid,
            "conflict_count":self.conflicts, "totals":self.totals,
            "billing":"UNKNOWN", "included_in_reported_usage_totals":false})
    }
}

impl RuntimeEvidenceTotals {
    pub fn add(&mut self, bound: bool, state: &RuntimeEvidenceState) {
        self.failed_usage.add(bound, state);
        for (total, value) in self.tokens.iter_mut().zip(state.tokens) {
            if total.checked_add(value).is_none() { self.unbound_or_malformed = true; }
            *total = total.saturating_add(value);
        }
        if !bound { self.unbound_or_malformed = true; return; }
        self.runtime_count += 1;
        self.valid_usage_count += u64::from(state.usage_records == 1 && state.usage_valid);
        let verified_pre_model_failure = state.verified_pre_model_failure();
        self.verified_pre_model_failure_count += u64::from(verified_pre_model_failure);
        self.model_usage_conflict |= state.observation_conflict
            || (state.pre_model_marker_seen && !verified_pre_model_failure);
        self.observation_count += u64::from(state.observation.is_some() && !state.observation_conflict);
        self.conflict_count += u64::from(state.observation_conflict);
    }

    pub fn usage(&self) -> serde_json::Value {
        let [input, cached, write, output, reasoning, total, latency] = self.tokens;
        // Reasoning may already be included in output; never assume it is disjoint.
        let total = if total == 0 { input.saturating_add(output) } else { total };
        // This accounting classification never fabricates a usage record or changes
        // the legacy recorded-runtime coverage denominator.
        let expected_usage_runtime_count = self.runtime_count - self.verified_pre_model_failure_count;
        let model_missing_usage_count = expected_usage_runtime_count - self.valid_usage_count;
        let model_usage_status = if self.runtime_count == 0 { "unknown" }
            else if self.unbound_or_malformed || self.model_usage_conflict { "incomplete" }
            else if expected_usage_runtime_count == 0 { "not_invoked" }
            else if model_missing_usage_count == 0 { "complete" } else { "incomplete" };
        let mut usage = serde_json::json!({"input_tokens":input, "cached_input_tokens":cached, "cache_write_tokens":write,
            "output_tokens":output, "reasoning_output_tokens":reasoning, "reasoning_tokens":reasoning,
            "total_tokens":total, "latency_ms":latency, "coverage":{
                "schema_version":"runtime_usage_coverage_v1",
                "scope":"recorded_session_context_runtimes_not_provider_attempts_or_billing",
                "known_runtime_count":self.runtime_count, "valid_usage_count":self.valid_usage_count,
                "missing_usage_count":self.runtime_count - self.valid_usage_count,
                "status":if self.runtime_count == 0 {"unknown"} else if self.runtime_count == self.valid_usage_count && !self.unbound_or_malformed {"complete"} else {"incomplete"}},
            "model_usage_coverage":{
                "schema_version":"runtime_model_usage_coverage_v1",
                "scope":"recorded_runtime_invocations_not_all_provider_attempts_or_billing",
                "verified_pre_model_failure_count":self.verified_pre_model_failure_count,
                "expected_usage_runtime_count":expected_usage_runtime_count,
                "missing_usage_count":model_missing_usage_count, "status":model_usage_status}});
        if self.failed_usage.attempts > 0 || self.failed_usage.conflicts > 0 {
            usage["reported_failed_attempt_usage"] = self.failed_usage.summary();
        }
        usage
    }

    pub fn provider_summary(&self, model: serde_json::Value, service_tier: serde_json::Value) -> serde_json::Value {
        serde_json::json!({"schema_version":"provider_observation_summary_v1", "source":"provider_response",
            "runtime_count":self.runtime_count, "observation_count":self.observation_count,
            "conflict_count":self.conflict_count, "model":model, "service_tier":service_tier})
    }
}

/// Values arrive sorted by the existing SQLite owner; exact distinct counts
/// need only one previous value and the same eight displayed values as before.
#[derive(Debug, Default)]
pub struct SortedObservedEvidence {
    previous: Option<String>,
    observed_count: u64,
    distinct_count: u64,
    values: Vec<String>,
}

impl SortedObservedEvidence {
    pub fn add(&mut self, value: Option<&str>) -> Result<(), String> {
        let Some(value) = value else { return Ok(()); };
        if self.previous.as_deref().is_some_and(|previous| previous > value) {
            return Err("provider evidence traversal is not monotonic".to_string());
        }
        self.observed_count += 1;
        if self.previous.as_deref() != Some(value) {
            self.distinct_count += 1;
            self.previous = Some(value.to_string());
            if self.values.len() < 8 { self.values.push(value.to_string()); }
        }
        Ok(())
    }

    pub fn summary(&self, runtime_count: u64) -> serde_json::Value {
        serde_json::json!({"value":if runtime_count > 0 && self.observed_count == runtime_count && self.distinct_count == 1 {
            self.previous.clone() } else { None }, "observed_count":self.observed_count,
            "distinct_count":self.distinct_count, "distinct_values":self.values})
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CreateSessionRequest {
    pub command_id: String,
    pub session_id: String,
    pub creation_command: SessionCommand,
    pub copy_context: bool,
    pub workspace: String,
    pub session_directory: String,
    pub name: String,
    pub created_at: i64,
    pub model: Option<String>,
    pub agent: Option<String>,
    pub session_type: String,
    pub kill_processes_on_start: bool,
    pub validator_enabled: bool,
    pub force_planning: bool,
    pub model_variant: Option<String>,
    pub model_acceleration_enabled: bool,
    pub disable_permission_restrictions: bool,
    pub use_last_tool_call_response: bool,
    pub auto_session_name: bool,
    #[serde(deserialize_with = "Option::deserialize")]
    pub initial_task_plan_patch: Option<lifecycle::SessionTaskPlanPatch>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ExecuteSessionCommandRequest {
    pub command_id: String,
    pub session_id: String,
    pub session_command: SessionCommand,
    #[serde(deserialize_with = "Option::deserialize")]
    pub message_projection: Option<SessionRecordProjection>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SessionMetadataPatch {
    #[serde(deserialize_with = "Option::deserialize")]
    pub name: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub model: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub agent: Option<String>,
    pub clear_agent: bool,
    #[serde(deserialize_with = "Option::deserialize")]
    pub session_type: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub kill_processes_on_start: Option<bool>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub validator_enabled: Option<bool>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub force_planning: Option<bool>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub disable_permission_restrictions: Option<bool>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub use_last_tool_call_response: Option<bool>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub auto_session_name: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct UpdateSessionRequest {
    pub command_id: String,
    pub session_id: String,
    pub metadata: SessionMetadataPatch,
    #[serde(deserialize_with = "Option::deserialize")]
    pub task_plan_patch: Option<lifecycle::SessionTaskPlanPatch>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct UpdateSessionTodosRequest {
    pub command_id: String,
    pub session_id: String,
    pub todos: Vec<serde_json::Value>,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SessionCommandResult {
    pub event: SessionEvent,
    pub projection: SessionProjection,
    pub session_name: Option<String>,
    pub message_count: u64,
    pub last_user_message_at: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeLifecycleIdentity {
    pub commander_session_id: String,
    pub transaction_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_mission_revision_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delegated_input_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub goal_id: Option<String>,
    #[serde(default)]
    pub operator_override: bool,
    pub dispatch_runtime_id: String,
    pub dispatch_lease_id: String,
    pub receipt_event_seq: u64,
}

impl RuntimeLifecycleIdentity {
    pub fn validate(&self) -> Result<(), String> {
        for (name, value) in [
            ("commander_session_id", self.commander_session_id.as_str()),
            ("transaction_id", self.transaction_id.as_str()),
            ("dispatch_runtime_id", self.dispatch_runtime_id.as_str()),
            ("dispatch_lease_id", self.dispatch_lease_id.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(format!("{name} must be non-empty"));
            }
        }
        for (name, value) in [("task_id", &self.task_id), ("goal_id", &self.goal_id)] {
            if value
                .as_deref()
                .is_some_and(|value| value.trim().is_empty())
            {
                return Err(format!("{name} must be null or non-empty"));
            }
        }
        for (name, value) in [
            (
                "parent_mission_revision_sha256",
                &self.parent_mission_revision_sha256,
            ),
            ("delegated_input_sha256", &self.delegated_input_sha256),
        ] {
            if value.as_deref().is_some_and(|value| {
                value.len() != 64
                    || !value
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            }) {
                return Err(format!("{name} must be null or lowercase SHA-256"));
            }
        }
        if self.parent_mission_revision_sha256.is_some() != self.delegated_input_sha256.is_some() {
            return Err("delegated lifecycle digests must be supplied together".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RegisterRuntimeRequest {
    pub runtime_id: String,
    pub session_id: String,
    #[serde(deserialize_with = "Option::deserialize")]
    pub fallback_from_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lifecycle: Option<RuntimeLifecycleIdentity>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ActivateRuntimeLeaseRequest {
    pub runtime_id: String,
    pub lease_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CommitRuntimeEventRequest {
    pub runtime_id: String,
    pub event_seq: u64,
    pub expected_revision: u64,
    pub lease_id: String,
    pub idempotency_key: String,
    pub event: RuntimeEvent,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SessionFeedCommandUpdate {
    pub message_id: String,
    pub part_id: String,
    pub command_run_id: String,
    pub command_id: String,
    #[serde(deserialize_with = "Option::deserialize")]
    pub provider_tool_call_id: Option<String>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub command_index: Option<u64>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub event_seq: Option<i64>,
    pub status: String,
    pub command: serde_json::Value,
    pub result: serde_json::Value,
    pub created_at: i64,
    pub updated_at: i64,
}

/// Frontend projection facts. Stable Runtime and Session facts are durable and
/// replayable; `AssistantTextDelta` is broadcast live with cursor zero and is
/// replaced by a durable `AgentMessage` containing the exact accumulated live
/// text for that runtime.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "event", rename_all = "snake_case", deny_unknown_fields)]
pub enum SessionFeedEvent {
    MessageUpserted {
        message: SessionRecordProjection,
    },
    AssistantTextDelta {
        message_id: String,
        part_id: String,
        delta: String,
        created_at: i64,
        updated_at: i64,
    },
    AgentMessage {
        message_id: String,
        part_id: String,
        reply_message: String,
        new_learning: String,
        #[serde(deserialize_with = "Option::deserialize")]
        runtime_status: Option<RuntimeProjection>,
        #[serde(deserialize_with = "Option::deserialize")]
        context_tokens: Option<ContextTokenStats>,
        #[serde(deserialize_with = "Option::deserialize")]
        usage: Option<UsageReport>,
        created_at: i64,
        updated_at: i64,
    },
    ToolCallUpdated {
        message_id: String,
        part_id: String,
        tool_name: String,
        call_id: String,
        state: serde_json::Value,
        #[serde(deserialize_with = "Option::deserialize")]
        metadata: Option<serde_json::Value>,
        #[serde(deserialize_with = "Option::deserialize")]
        runtime_status: Option<RuntimeProjection>,
        #[serde(deserialize_with = "Option::deserialize")]
        context_tokens: Option<ContextTokenStats>,
        #[serde(deserialize_with = "Option::deserialize")]
        usage: Option<UsageReport>,
        command_updates: Vec<SessionFeedCommandUpdate>,
        created_at: i64,
        updated_at: i64,
    },
    TodosUpdated {
        todos: Vec<serde_json::Value>,
        updated_at: i64,
    },
    SessionProjectionUpdated {
        projection: SessionProjection,
        #[serde(deserialize_with = "Option::deserialize")]
        session_name: Option<String>,
        updated_at: i64,
    },
    SessionSnapshotCreated {
        snapshot: Box<SessionSnapshot>,
    },
    SessionSnapshotUpdated {
        snapshot: Box<SessionSnapshot>,
    },
    SessionDeleted {},
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct AppendSessionFeedEventRequest {
    pub runtime_id: String,
    pub target_session_id: String,
    pub lease_id: String,
    pub event_id: String,
    pub event: SessionFeedEvent,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReadSessionFeedRequest {
    pub session_id: String,
    pub after_cursor: u64,
    pub limit: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SessionFeedEntry {
    pub session_id: String,
    /// Durable entries use monotonically increasing cursors starting at one.
    /// Cursor zero marks a live-only event that must not advance replay state.
    pub cursor: u64,
    #[serde(deserialize_with = "Option::deserialize")]
    pub runtime_id: Option<String>,
    pub event_id: String,
    pub event: SessionFeedEvent,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "outcome", rename_all = "snake_case", deny_unknown_fields)]
pub enum SessionFeedAppendOutcome {
    Applied { cursor: u64 },
    Duplicate { cursor: u64 },
    PublishedLive,
    RuntimeNotFound,
    TargetSessionNotFound,
    TargetWorkspaceMismatch,
    StaleLease,
    RuntimeTerminal,
    EventIdConflict,
    SessionOwnedEvent,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReplayRuntimeRequest {
    pub runtime_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct GetRuntimeLeaseRequest {
    pub runtime_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub database_path: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ListRuntimeLocationsRequest {
    #[serde(default)]
    pub page: u64,
    #[serde(default = "default_page_size")]
    pub page_size: u64,
    /// Stable keyset cursor for recovery scans whose result set may shrink as rows are closed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub after_runtime_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeLocation {
    pub runtime_id: String,
    pub session_id: String,
    pub workspace_db_path: String,
    #[serde(default)]
    pub terminal_proven: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub terminal_revision: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub terminal_event_seq: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub terminal_evidence_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeLeaseSnapshot {
    pub database_path: String,
    pub runtime_id: String,
    pub session_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lifecycle: Option<RuntimeLifecycleIdentity>,
    #[serde(deserialize_with = "Option::deserialize")]
    pub lease_id: Option<String>,
    pub lease_active: bool,
    pub revision: u64,
    pub last_event_seq: u64,
    pub terminal: bool,
    pub session_event_seq: u64,
    pub session_state: SessionState,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub runtime_state: Option<RuntimeState>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryCloseRuntimeReason {
    OrphanedRuntime,
    UnbornRuntime,
    CommanderConvergenceProven,
}

pub fn recovery_terminal_projection_event_id(receipt_id: &str) -> String {
    format!("runtime-recovery:{receipt_id}:session-projection")
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeRecoveryQuiescenceProof {
    pub active_turn: bool,
    pub queued_turn: bool,
    pub running_turn: bool,
    pub worker_alive: bool,
    pub active_command_runs: u64,
    pub retained_process_scopes: u64,
    pub retained_slot: bool,
    pub global_active_session_count: u64,
    pub global_retained_slot_count: u64,
    pub global_active_command_runs: u64,
}

impl RuntimeRecoveryQuiescenceProof {
    pub fn is_quiescent(&self) -> bool {
        !self.active_turn
            && !self.queued_turn
            && !self.running_turn
            && !self.worker_alive
            && self.active_command_runs == 0
            && self.retained_process_scopes == 0
            && !self.retained_slot
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecoveryCloseRuntimeRequest {
    pub receipt_id: String,
    pub database_path: String,
    pub runtime_id: String,
    pub session_id: String,
    #[serde(deserialize_with = "Option::deserialize")]
    pub lease_id: Option<String>,
    pub expected_lease_active: bool,
    pub expected_revision: u64,
    pub expected_last_event_seq: u64,
    pub expected_session_event_seq: u64,
    pub expected_session_state: SessionState,
    pub reason: RecoveryCloseRuntimeReason,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub convergence_proof_sha256: Option<String>,
    pub quiescence: RuntimeRecoveryQuiescenceProof,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeRecoveryReceipt {
    pub receipt_id: String,
    pub database_path: String,
    pub runtime_id: String,
    pub session_id: String,
    #[serde(deserialize_with = "Option::deserialize")]
    pub lease_id: Option<String>,
    pub revision: u64,
    pub last_event_seq: u64,
    pub session_event_seq: u64,
    pub reason: RecoveryCloseRuntimeReason,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub convergence_proof_sha256: Option<String>,
    pub lease_active: bool,
    pub terminal: bool,
    pub session_state: SessionState,
    pub quiescence: RuntimeRecoveryQuiescenceProof,
    pub closed_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "outcome", rename_all = "snake_case", deny_unknown_fields)]
pub enum RecoveryCloseRuntimeOutcome {
    Closed {
        receipt: RuntimeRecoveryReceipt,
    },
    AlreadyClosed {
        receipt: RuntimeRecoveryReceipt,
    },
    RuntimeLive {
        proof: RuntimeRecoveryQuiescenceProof,
    },
    RuntimeNotFound,
    IdentityMismatch {
        field: String,
    },
    CasConflict {
        current_revision: u64,
        current_last_event_seq: u64,
    },
    LeaseStateConflict {
        lease_active: bool,
        terminal: bool,
    },
    SessionCasConflict {
        current_event_seq: u64,
        current_state: SessionState,
    },
    InvalidRecoveryShape {
        error: String,
    },
    ReceiptConflict,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "outcome", rename_all = "snake_case", deny_unknown_fields)]
pub enum RuntimeRegistrationOutcome {
    Registered {
        revision: u64,
        next_event_seq: u64,
        projection: SessionProjection,
    },
    AlreadyRegistered {
        revision: u64,
        next_event_seq: u64,
        projection: SessionProjection,
    },
    SessionBusy {
        active_runtime_id: String,
    },
    RuntimeIdConflict,
    SessionNotFound,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "outcome", rename_all = "snake_case", deny_unknown_fields)]
pub enum RuntimeLeaseOutcome {
    Activated,
    AlreadyActive,
    RuntimeNotFound,
    RuntimeTerminal,
    LeaseConflict,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "outcome", rename_all = "snake_case", deny_unknown_fields)]
pub enum RuntimeEventCommitOutcome {
    Applied {
        revision: u64,
        next_event_seq: u64,
        projection: RuntimeProjection,
    },
    Duplicate {
        revision: u64,
        next_event_seq: u64,
    },
    RuntimeNotFound,
    StaleLease,
    RuntimeTerminal,
    OutOfOrder {
        expected_event_seq: u64,
        received_event_seq: u64,
    },
    RevisionConflict {
        current_revision: u64,
        expected_revision: u64,
    },
    InvalidEvent {
        error: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeReplay {
    pub aggregate: RuntimeAggregate,
    pub revision: u64,
    pub next_event_seq: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ListSessionsRequest {
    pub workspace: String,
    #[serde(default)]
    pub page: u64,
    #[serde(default = "default_page_size")]
    pub page_size: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ListSessionRecordsRequest {
    pub session_id: String,
    #[serde(default)]
    pub page: u64,
    #[serde(default = "default_page_size")]
    pub page_size: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GetSessionRequest {
    pub session_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeleteSessionRequest {
    pub session_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MarkSessionInterruptedRequest {
    pub session_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeleteWorkspaceRequest {
    pub workspace: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RuntimeLocationMaintenanceMode {
    DryRun,
    Apply,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MaintainRuntimeLocationsRequest {
    pub mode: RuntimeLocationMaintenanceMode,
    #[serde(default = "default_page_size")]
    pub page_size: u64,
    #[serde(default)]
    pub expected_dry_run_sha256: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeLocationMaintenanceReceipt {
    pub schema_version: String,
    pub mode: RuntimeLocationMaintenanceMode,
    pub canonical_input_sha256: String,
    pub pre_incomplete_count: u64,
    pub post_incomplete_count: u64,
    pub backfilled_count: u64,
    pub deleted_count: u64,
    pub mutation_count: u64,
    pub active_lease_count: u64,
    pub dispositions: std::collections::BTreeMap<String, u64>,
    pub replayed_receipt: bool,
    pub manifest_path: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
pub enum SessionLogCommand {
    Health,
    CreateSession(Box<CreateSessionRequest>),
    ExecuteSessionCommand(ExecuteSessionCommandRequest),
    UpdateSession(UpdateSessionRequest),
    UpdateSessionTodos(UpdateSessionTodosRequest),
    RegisterRuntime(RegisterRuntimeRequest),
    ActivateRuntimeLease(ActivateRuntimeLeaseRequest),
    CommitRuntimeEvent(CommitRuntimeEventRequest),
    AppendSessionFeedEvent(AppendSessionFeedEventRequest),
    ReadSessionFeed(ReadSessionFeedRequest),
    SubscribeSessionFeed,
    ReplayRuntime(ReplayRuntimeRequest),
    GetRuntimeLease(GetRuntimeLeaseRequest),
    ListRuntimeLocations(ListRuntimeLocationsRequest),
    MaintainRuntimeLocations(MaintainRuntimeLocationsRequest),
    RecoveryCloseRuntime(RecoveryCloseRuntimeRequest),
    PersistSessionDelta(Box<PersistSessionDeltaRequest>),
    ReadContextSlice(ReadContextSliceRequest),
    ReadExecutionEvidence(ReadExecutionEvidenceRequest),
    ApplyCommandCheckpoint(Box<CommandCheckpoint>),
    GetSession(GetSessionRequest),
    ListWorkspaces,
    ListSessions(ListSessionsRequest),
    ListSessionSummaries(ListSessionsRequest),
    ListSessionRecords(ListSessionRecordsRequest),
    MarkSessionInterrupted(MarkSessionInterruptedRequest),
    DeleteSession(DeleteSessionRequest),
    DeleteWorkspace(DeleteWorkspaceRequest),
    Shutdown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum SessionLogResponse {
    Ok,
    SessionCommandApplied {
        result: Box<SessionCommandResult>,
    },
    SessionUpdated {
        session: Box<SessionSnapshot>,
    },
    SessionTodosUpdated {
        todos: Vec<serde_json::Value>,
        cursor: u64,
    },
    RuntimeRegistered {
        result: RuntimeRegistrationOutcome,
    },
    RuntimeLeaseActivated {
        result: RuntimeLeaseOutcome,
    },
    RuntimeEventCommitted {
        result: RuntimeEventCommitOutcome,
    },
    SessionFeedEventAppended {
        result: SessionFeedAppendOutcome,
    },
    SessionFeed {
        entries: Vec<SessionFeedEntry>,
        next_cursor: u64,
    },
    SessionFeedSubscribed,
    SessionFeedEvent {
        entry: Box<SessionFeedEntry>,
    },
    RuntimeReplayed {
        runtime: Option<Box<RuntimeReplay>>,
    },
    RuntimeLeaseRead {
        runtime: Option<RuntimeLeaseSnapshot>,
    },
    RuntimeLocations {
        page: Page,
        locations: Vec<RuntimeLocation>,
    },
    RuntimeLocationsMaintained {
        receipt: RuntimeLocationMaintenanceReceipt,
    },
    RuntimeRecoveryClosed {
        result: RecoveryCloseRuntimeOutcome,
    },
    SessionDeltaPersisted {
        next_sequence: u64,
        next_management_sequence: u64,
    },
    ContextSlice {
        context: ContextSlice,
    },
    ExecutionEvidence {
        evidence: ExecutionEvidencePage,
    },
    Workspaces {
        workspaces: Vec<WorkspaceSummary>,
    },
    Sessions {
        page: Page,
        sessions: Vec<SessionSnapshot>,
    },
    SessionSummaries {
        page: Page,
        sessions: Vec<SessionSummary>,
    },
    Session {
        session: Option<Box<SessionSnapshot>>,
    },
    Records {
        page: Page,
        records: Vec<SessionRecord>,
    },
    Error {
        error: String,
    },
}

#[cfg(test)]
mod tests {
    use super::{
        CreateSessionRequest, DeleteSessionRequest, DeleteWorkspaceRequest,
        ExecuteSessionCommandRequest, GetSessionRequest, ListSessionRecordsRequest,
        ListSessionsRequest, MarkSessionInterruptedRequest, Page, PersistSessionDeltaRequest,
        RegisterRuntimeRequest, SessionCommandResult, SessionFeedEvent, SessionLogCommand,
        SessionLogResponse, SessionMetadata, SessionMetadataPatch, SessionRecord, SessionSnapshot,
        UpdateSessionRequest, WorkspaceSummary,
    };
    use lifecycle::{
        SessionAggregate, SessionCommand, SessionEvent, SessionInput, SessionManagement,
        SessionQuery,
    };
    use serde_json::json;

    fn pre_model_failure_evidence() -> serde_json::Value {
        json!({"type":"runtime_provider_observation", "runtime_id":"auth", "session_id":"session",
            "status":"Failed", "error_code":"CALL_FAILED", "provider_observation":null,
            "provider_call_observation":{"schema_version":"provider_call_observation_v1",
                "scope":"runtime_provider_call", "stage":"authentication", "model_provider_invoked":false}})
    }

    fn usage_evidence() -> serde_json::Value {
        json!({"type":"runtime_usage", "runtime_id":"model",
            "usage":{"input_tokens":10,"output_tokens":2,"total_tokens":12}})
    }

    fn evidence_usage(records: &[serde_json::Value]) -> serde_json::Value {
        let mut state = super::RuntimeEvidenceState::default();
        for record in records { state.add(record); }
        let mut totals = super::RuntimeEvidenceTotals::default();
        totals.add(true, &state);
        totals.usage()
    }

    #[test]
    fn failure_usage_is_separate_deduplicated_and_never_complete_billing() {
        let record = serde_json::json!({"type":"runtime_provider_observation","runtime_id":"a",
            "provider_observation":{"schema_version":"provider_observation_v1","source":"provider_response",
                "failed_attempts":[{"usage":{"input_tokens":10,"output_tokens":2,"total_tokens":12,
                    "cached_input_tokens":0,"invalid_fields":[]}}, {"usage":null}]}});
        let final_usage = serde_json::json!({"type":"runtime_usage","runtime_id":"a",
            "usage":{"input_tokens":20,"output_tokens":4,"total_tokens":24}});
        let result = evidence_usage(&[record.clone(), record.clone(), final_usage]);
        assert_eq!(result["total_tokens"], 24);
        let failed = &result["reported_failed_attempt_usage"];
        assert_eq!(failed["attempt_count"], 2);
        assert_eq!(failed["totals"]["total_tokens"], 12);
        assert_eq!(failed["totals"]["cached_input_tokens"], 0);
        assert!(failed["totals"].get("reasoning_tokens").is_none());
        assert_eq!(failed["unknown_attempt_count"], 1);
        assert_eq!(failed["coverage"], "partial");
        assert_eq!(failed["billing"], "UNKNOWN");
        assert_eq!(failed["included_in_reported_usage_totals"], false);
        let mut conflicting = record.clone();
        conflicting["provider_observation"]["failed_attempts"] = serde_json::json!([]);
        let result = evidence_usage(&[record, conflicting]);
        assert_eq!(result["reported_failed_attempt_usage"]["conflict_count"], 1);
        assert_eq!(result["reported_failed_attempt_usage"]["totals"], serde_json::json!({}));
    }

    #[test]
    fn failure_usage_rejects_invalid_subsets_without_fabricated_zero_totals() {
        for usage in [
            serde_json::json!({"input_tokens":-1,"invalid_fields":[]}),
            serde_json::json!({"input_tokens":true,"invalid_fields":[]}),
            serde_json::json!({"input_tokens":10,"output_tokens":2,"total_tokens":11,"invalid_fields":[]}),
            serde_json::json!({"input_tokens":10,"cached_input_tokens":11,"invalid_fields":[]}),
        ] {
            let result = evidence_usage(&[serde_json::json!({"type":"runtime_provider_observation",
                "runtime_id":"a","provider_observation":{"schema_version":"provider_observation_v1",
                    "source":"provider_failure","failed_attempts":[{"usage":usage}]}})]);
            assert_eq!(result["reported_failed_attempt_usage"]["invalid_attempt_count"], 1);
            assert_eq!(result["reported_failed_attempt_usage"]["totals"], serde_json::json!({}));
        }
    }

    #[test]
    fn model_usage_coverage_separates_verified_pre_model_failures_without_usage() {
        let mut auth = super::RuntimeEvidenceState::default();
        auth.add(&pre_model_failure_evidence());
        assert_eq!(auth.usage_records, 0);
        assert!(!auth.usage_valid);
        let mut totals = super::RuntimeEvidenceTotals::default();
        totals.add(true, &auth);
        let auth_only = totals.usage();
        assert_eq!(auth_only["coverage"], json!({"schema_version":"runtime_usage_coverage_v1",
            "scope":"recorded_session_context_runtimes_not_provider_attempts_or_billing",
            "known_runtime_count":1,"valid_usage_count":0,"missing_usage_count":1,"status":"incomplete"}));
        assert_eq!(auth_only["model_usage_coverage"], json!({"schema_version":"runtime_model_usage_coverage_v1",
            "scope":"recorded_runtime_invocations_not_all_provider_attempts_or_billing",
            "verified_pre_model_failure_count":1,"expected_usage_runtime_count":0,"missing_usage_count":0,"status":"not_invoked"}));
        let mut model = super::RuntimeEvidenceState::default();
        model.add(&usage_evidence());
        totals.add(true, &model);
        let combined = totals.usage();
        assert_eq!(combined["total_tokens"], 12);
        assert_eq!(combined["coverage"]["status"], "incomplete");
        assert_eq!(combined["coverage"]["known_runtime_count"], 2);
        assert_eq!(combined["coverage"]["valid_usage_count"], 1);
        assert_eq!(combined["model_usage_coverage"]["expected_usage_runtime_count"], 1);
        assert_eq!(combined["model_usage_coverage"]["status"], "complete");
    }

    #[test]
    fn model_usage_coverage_requires_exact_failed_authentication_marker() {
        let marker = pre_model_failure_evidence();
        let mut invalid = Vec::new();
        for field in ["schema_version", "scope", "stage", "model_provider_invoked"] {
            let mut value = marker.clone();
            value["provider_call_observation"].as_object_mut().unwrap().remove(field);
            invalid.push(value);
        }
        for (field, value) in [
            ("schema_version", json!("provider_call_observation_v2")),
            ("scope", json!("provider_attempt")), ("stage", json!("model")),
            ("model_provider_invoked", json!(true)), ("model_provider_invoked", json!("false")),
            ("model_provider_invoked", json!(0)), ("model_provider_invoked", json!(null)),
            ("extra", json!(false)),
        ] {
            let mut record = marker.clone();
            record["provider_call_observation"][field] = value;
            invalid.push(record);
        }
        for (field, value) in [("status", json!("Succeeded")), ("status", json!(null)),
            ("error_code", json!("CALL_FAILED_AFTER_COMMAND_EFFECT")), ("error_code", json!(null))] {
            let mut record = marker.clone();
            record[field] = value;
            invalid.push(record);
        }
        for malformed in [json!(false), json!("false"), json!([])] {
            let mut record = marker.clone();
            record["provider_call_observation"] = malformed;
            invalid.push(record);
        }
        for field in ["runtime_id", "session_id"] {
            let mut missing = marker.clone();
            missing.as_object_mut().unwrap().remove(field);
            invalid.push(missing);
            for malformed in [json!(null), json!(false), json!(1), json!(""), json!(" \t "), json!("bad\nid")] {
                let mut record = marker.clone();
                record[field] = malformed;
                invalid.push(record);
            }
        }
        for record in invalid {
            let result = evidence_usage(std::slice::from_ref(&record));
            assert_eq!(result["model_usage_coverage"]["verified_pre_model_failure_count"], 0, "{record}");
            assert_eq!(result["model_usage_coverage"]["status"], "incomplete", "{record}");
            assert_eq!(result["coverage"]["missing_usage_count"], 1);
            assert_eq!(evidence_usage(&[record, usage_evidence()])["model_usage_coverage"]["status"], "incomplete");
        }
    }

    #[test]
    fn model_usage_coverage_rejects_contradictions_and_duplicates_in_either_order() {
        let marker = pre_model_failure_evidence();
        let mut zero = usage_evidence();
        zero["usage"] = json!({"input_tokens":0,"output_tokens":0,"total_tokens":0});
        let response = json!({"type":"runtime_provider_observation", "runtime_id":"auth",
            "provider_observation":{"schema_version":"provider_observation_v1", "source":"provider_response", "response_id":"response"}});
        for mut other in [marker.clone(), usage_evidence(), zero, response] {
            other["runtime_id"] = json!("auth");
            for records in [vec![marker.clone(), other.clone()], vec![other.clone(), marker.clone()]] {
                let result = evidence_usage(&records);
                assert_eq!(result["model_usage_coverage"]["verified_pre_model_failure_count"], 0);
                assert_eq!(result["model_usage_coverage"]["status"], "incomplete");
            }
        }
        for response in [json!({}), json!(false), json!({"schema_version":"provider_observation_v1", "source":"provider_response"})] {
            let mut record = marker.clone();
            record["provider_observation"] = response;
            assert_eq!(evidence_usage(&[record])["model_usage_coverage"]["status"], "incomplete");
        }
        assert_eq!(evidence_usage(&[usage_evidence(), json!({"type":"runtime_provider_observation",
            "runtime_id":"model", "provider_observation":false})])["model_usage_coverage"]["status"], "incomplete");
    }

    #[test]
    fn model_usage_coverage_does_not_infer_legacy_failures_or_unbound_calls() {
        let empty = super::RuntimeEvidenceTotals::default().usage();
        assert_eq!(empty["model_usage_coverage"]["status"], "unknown");
        for observation in [serde_json::Value::Null,
            json!({"schema_version":"provider_observation_v1", "source":"provider_response", "model":"gpt-6.1-sol"})] {
            let record = json!({"type":"runtime_provider_observation", "runtime_id":"missing",
                "status":"Failed", "error_code":"CALL_FAILED", "error":"OAuth authentication before model",
                "provider_observation":observation});
            let result = evidence_usage(&[record]);
            assert_eq!(result["model_usage_coverage"]["expected_usage_runtime_count"], 1);
            assert_eq!(result["model_usage_coverage"]["missing_usage_count"], 1);
            assert_eq!(result["model_usage_coverage"]["status"], "incomplete");
        }
        let mut model = super::RuntimeEvidenceState::default();
        model.add(&usage_evidence());
        let mut auth = super::RuntimeEvidenceState::default();
        auth.add(&pre_model_failure_evidence());
        let mut totals = super::RuntimeEvidenceTotals::default();
        totals.add(true, &model);
        totals.add(false, &auth);
        assert_eq!(totals.usage()["model_usage_coverage"]["verified_pre_model_failure_count"], 0);
        assert_eq!(totals.usage()["model_usage_coverage"]["status"], "incomplete");
        assert_eq!(evidence_usage(&[usage_evidence()])["model_usage_coverage"]["status"], "complete");
    }

    #[test]
    fn runtime_usage_invalid_subsets_cannot_be_complete() {
        for usage in [
            json!({"input_tokens":10,"output_tokens":2,"total_tokens":11}),
            json!({"input_tokens":u64::MAX,"output_tokens":1,"total_tokens":u64::MAX}),
        ] {
            let mut state = super::RuntimeEvidenceState::default();
            state.add(&json!({"type":"runtime_usage","runtime_id":"a","usage":usage}));
            let mut totals = super::RuntimeEvidenceTotals::default();
            totals.add(true, &state);
            assert!(!state.usage_valid);
            assert_eq!(totals.usage()["coverage"]["status"], "incomplete");
        }
    }

    #[test]
    fn runtime_usage_missing_total_does_not_assume_disjoint_reasoning() {
        let mut state = super::RuntimeEvidenceState::default();
        state.add(&json!({"type":"runtime_usage","runtime_id":"a",
            "usage":{"input_tokens":2,"output_tokens":3,"reasoning_tokens":2}}));
        let mut totals = super::RuntimeEvidenceTotals::default();
        totals.add(true, &state);
        let usage = totals.usage();
        assert_eq!(usage["total_tokens"], 5);
        assert_eq!(usage["reasoning_tokens"], 2);
        assert_eq!(usage["coverage"]["status"], "incomplete");
    }

    #[test]
    fn runtime_usage_additive_cache_and_reasoning_remain_reported_not_guessed() {
        let mut state = super::RuntimeEvidenceState::default();
        state.add(&json!({"type":"runtime_usage","runtime_id":"a",
            "usage":{"input_tokens":10,"output_tokens":2,"cached_input_tokens":11,
                     "reasoning_tokens":3,"total_tokens":26}}));
        assert!(state.usage_valid);
        let mut totals = super::RuntimeEvidenceTotals::default();
        totals.add(true, &state);
        assert_eq!(totals.usage()["total_tokens"], 26);
    }

    #[test]
    fn runtime_usage_aggregate_overflow_cannot_be_complete() {
        let mut state = super::RuntimeEvidenceState::default();
        state.add(&json!({"type":"runtime_usage","runtime_id":"a",
            "usage":{"input_tokens":u64::MAX / 2 + 1,"output_tokens":0,"total_tokens":u64::MAX / 2 + 1}}));
        assert!(state.usage_valid);
        let mut totals = super::RuntimeEvidenceTotals::default();
        totals.add(true, &state);
        totals.add(true, &state);
        assert_eq!(totals.usage()["coverage"]["status"], "incomplete");
    }

    fn snapshot_fixture(session_id: &str) -> SessionSnapshot {
        let timestamp =
            chrono::DateTime::<chrono::Utc>::from_timestamp_millis(1).expect("snapshot timestamp");
        let projection =
            SessionAggregate::new(session_id.to_string()).query(SessionQuery::Lifecycle);
        let mut management = SessionManagement::new(
            session_id.to_string(),
            "Session".to_string(),
            "workspace".into(),
            false,
            Vec::<String>::new(),
            SessionInput {
                user_input: String::new(),
                file_input: Vec::new(),
                agent: None,
                runtime_context: None,
                planning_mode_override: None,
            },
            String::new(),
            timestamp,
        );
        management.replace_lifecycle_projection(projection.clone());
        SessionSnapshot {
            session_id: session_id.to_string(),
            workspace: "workspace".to_string(),
            name: Some(management.session_name.clone()),
            created_at: 1,
            updated_at: 2,
            last_user_message_at: Some(1),
            message_count: 1,
            lifecycle_projection: projection,
            metadata: SessionMetadata {
                session_directory: "workspace".to_string(),
                model: None,
                agent: None,
                session_type: "coding".to_string(),
                kill_processes_on_start: false,
                validator_enabled: false,
                force_planning: false,
                model_variant: None,
                model_acceleration_enabled: false,
                disable_permission_restrictions: management.disable_permission_restrictions,
                use_last_tool_call_response: management.use_last_tool_call_response,
                auto_session_name: management.auto_session_name,
                context_tokens: management.context_tokens,
                runtime_usage: management.runtime_usage.clone(),
            },
            management,
            todos: Vec::new(),
        }
    }

    #[test]
    fn page_defaults_match_public_pagination_contract() {
        assert_eq!(
            Page::default(),
            Page {
                page: 0,
                page_size: 50,
                total: 0
            }
        );
        let page: Page = serde_json::from_value(json!({ "total": 7 })).expect("page");
        assert_eq!(page.page, 0);
        assert_eq!(page.page_size, 50);
        assert_eq!(page.total, 7);
    }

    #[test]
    fn execution_evidence_visits_complete_history_in_bounded_pages_and_checks_terminal_cursor() {
        use std::cell::Cell;
        let snapshot = super::ExecutionEvidenceSnapshot {
            session_id: "paged-history".to_string(), next_sequence: 1025,
            next_management_sequence: 3, retained_from_sequence: 1000,
        };
        let seen = Cell::new(0_u64);
        let largest = Cell::new(0_usize);
        let calls = Cell::new(0_usize);
        let terminal = Cell::new(false);
        super::visit_execution_evidence(&snapshot, |request| {
            assert_eq!(request.from_sequence, seen.get());
            assert_eq!(request.snapshot.as_ref(), Some(&snapshot));
            let end = (request.from_sequence + request.max_records).min(snapshot.next_sequence);
            let records = (request.from_sequence..end).map(|sequence| super::SessionContextRecord {
                sequence, raw_record: json!({"session_id":"paged-history", "content":"fixture"}).to_string(),
            }).collect::<Vec<_>>();
            largest.set(largest.get().max(records.len()));
            calls.set(calls.get() + 1);
            terminal.set(request.from_sequence == snapshot.next_sequence);
            Ok(super::ExecutionEvidencePage { snapshot: snapshot.clone(), next_sequence: end, records, summary: None })
        }, |record| {
            assert_eq!(record.sequence, seen.get());
            seen.set(seen.get() + 1);
            Ok(())
        }).expect("bounded complete evidence traversal");
        assert_eq!(seen.get(), 1025);
        assert!(largest.get() <= super::EXECUTION_EVIDENCE_PAGE_RECORDS as usize);
        assert_eq!(calls.get(), 10); // nine data pages plus an independently checked terminal page
        assert!(terminal.get());
        let terminal_drift = Cell::new(false);
        let result = super::visit_execution_evidence(&snapshot, |request| {
            let end = (request.from_sequence + request.max_records).min(snapshot.next_sequence);
            let mut actual = snapshot.clone();
            if request.from_sequence == snapshot.next_sequence {
                terminal_drift.set(true);
                actual.next_management_sequence += 1;
            }
            Ok(super::ExecutionEvidencePage { snapshot:actual, next_sequence:end,
                records:(request.from_sequence..end).map(|sequence| super::SessionContextRecord {
                    sequence, raw_record:json!({"content":"fixture"}).to_string() }).collect(), summary:None })
        }, |_| Ok(()));
        assert!(terminal_drift.get());
        assert!(result.is_err(), "a drift after the last data page is not completion");
    }

    #[test]
    fn execution_evidence_rejects_identity_gaps_duplicates_drift_and_unbounded_pages() {
        let snapshot = super::ExecutionEvidenceSnapshot { session_id:"identity".to_string(),
            next_sequence:2, next_management_sequence:3, retained_from_sequence:1 };
        let request = super::ReadExecutionEvidenceRequest { session_id:snapshot.session_id.clone(),
            snapshot:Some(snapshot.clone()), from_sequence:0, max_records:2,
            max_bytes:super::EXECUTION_EVIDENCE_PAGE_BYTES, include_summary:false };
        let valid = super::ExecutionEvidencePage { snapshot:snapshot.clone(), next_sequence:2,
            records:(0..2).map(|sequence| super::SessionContextRecord { sequence,
                raw_record:json!({"session_id":"identity","content":"fixture"}).to_string() }).collect(), summary:None };
        valid.validate(&request).expect("valid complete page");
        for case in 0..8 {
            let mut page = valid.clone();
            match case {
                0 => page.snapshot.session_id = "other".to_string(),
                1 => page.snapshot.next_management_sequence += 1,
                2 => page.records[0].sequence = 1,
                3 => page.records[1].sequence = 0,
                4 => { page.records.clear(); page.next_sequence = 0; },
                5 => page.records[0].raw_record = json!({"session_id":"other"}).to_string(),
                6 => page.records[0].raw_record = "not JSON".to_string(),
                _ => page.next_sequence = 1,
            }
            assert!(page.validate(&request).is_err(), "must fail closed for case {case}");
        }
        let mut small = request.clone();
        small.max_bytes = 1;
        assert!(valid.validate(&small).is_err());
        small.max_records = 0;
        assert!(small.validate().is_err());
        let mut wrong = request.clone();
        wrong.session_id = "other".to_string();
        assert!(wrong.validate().is_err());
        let mut changed = snapshot.clone();
        changed.next_sequence += 1;
        assert!(super::visit_execution_evidence(&snapshot, |_| Ok(super::ExecutionEvidencePage {
            snapshot:changed.clone(), next_sequence:0, records:Vec::new(), summary:None
        }), |_| panic!("drifted evidence must not reach projection")).is_err());
    }

    #[test]
    fn request_defaults_keep_optional_payloads_empty() {
        let list: ListSessionsRequest =
            serde_json::from_value(json!({ "workspace": "workspace" })).expect("list sessions");
        assert_eq!(list.page, 0);
        assert_eq!(list.page_size, 50);

        let records: ListSessionRecordsRequest =
            serde_json::from_value(json!({ "session_id": "session" })).expect("records");
        assert_eq!(records.page, 0);
        assert_eq!(records.page_size, 50);

        let register = RegisterRuntimeRequest {
            runtime_id: "runtime-retry".to_string(),
            session_id: "session".to_string(),
            fallback_from_id: Some("runtime-failed".to_string()),
            lifecycle: None,
        };
        let value = serde_json::to_value(&register).expect("register runtime request");
        assert_eq!(value["fallback_from_id"], "runtime-failed");
        assert_eq!(
            serde_json::from_value::<RegisterRuntimeRequest>(value)
                .expect("register runtime round trip"),
            register
        );
        assert!(
            serde_json::from_value::<RegisterRuntimeRequest>(json!({
                "runtime_id": "runtime",
                "session_id": "session"
            }))
            .is_err()
        );
    }

    #[test]
    fn command_serde_uses_snake_case_tagged_contract() {
        let commands = [
            SessionLogCommand::Health,
            SessionLogCommand::ExecuteSessionCommand(ExecuteSessionCommandRequest {
                command_id: "command".to_string(),
                session_id: "session".to_string(),
                session_command: SessionCommand::SubmitUserInput,
                message_projection: None,
            }),
            SessionLogCommand::UpdateSession(UpdateSessionRequest {
                command_id: "update-command".to_string(),
                session_id: "session".to_string(),
                metadata: SessionMetadataPatch {
                    name: Some("Updated".to_string()),
                    ..SessionMetadataPatch::default()
                },
                task_plan_patch: None,
            }),
            SessionLogCommand::GetSession(GetSessionRequest {
                session_id: "session".to_string(),
            }),
            SessionLogCommand::DeleteSession(DeleteSessionRequest {
                session_id: "session".to_string(),
            }),
            SessionLogCommand::MarkSessionInterrupted(MarkSessionInterruptedRequest {
                session_id: "session".to_string(),
            }),
            SessionLogCommand::DeleteWorkspace(DeleteWorkspaceRequest {
                workspace: "workspace".to_string(),
            }),
            SessionLogCommand::Shutdown,
        ];

        for command in commands {
            let value = serde_json::to_value(&command).expect("command json");
            let command_name = value["command"].as_str().expect("command tag");
            assert!(
                command_name
                    .chars()
                    .all(|ch| ch.is_ascii_lowercase() || ch == '_'),
                "command tag must stay snake_case: {command_name}"
            );
            if command_name == "execute_session_command" {
                assert_eq!(
                    value["session_command"],
                    json!({ "command": "submit_user_input" })
                );
            }
            let round_trip: SessionLogCommand =
                serde_json::from_value(value).expect("command round trip");
            assert_eq!(
                std::mem::discriminant(&round_trip),
                std::mem::discriminant(&command)
            );
        }
    }

    #[test]
    fn boxed_session_delta_keeps_the_flat_wire_contract() {
        let now =
            serde_json::from_value(json!("2026-07-20T00:00:00Z")).expect("valid session timestamp");
        let management = SessionManagement::new(
            "session".to_string(),
            "Session".to_string(),
            "C:/workspace".into(),
            false,
            Vec::<String>::new(),
            SessionInput {
                user_input: "hello".to_string(),
                file_input: Vec::new(),
                agent: None,
                runtime_context: None,
                planning_mode_override: None,
            },
            "goal".to_string(),
            now,
        );
        let command =
            SessionLogCommand::PersistSessionDelta(Box::new(PersistSessionDeltaRequest {
                session_id: "session".to_string(),
                management_sequence: 0,
                management_delta: SessionManagement::persistence_delta(None, &management),
                retained_from_sequence: 0,
                entries: Vec::new(),
            }));

        let value = serde_json::to_value(&command).expect("session delta command json");
        assert_eq!(value["command"], "persist_session_delta");
        assert_eq!(value["session_id"], "session");
        assert_eq!(value["management_sequence"], 0);
        assert_eq!(value["management_delta"]["session_name"], "Session");
        assert_eq!(value["retained_from_sequence"], 0);
        assert!(value.get("payload").is_none());

        let round_trip: SessionLogCommand =
            serde_json::from_value(value).expect("session delta command round trip");
        assert!(matches!(
            round_trip,
            SessionLogCommand::PersistSessionDelta(payload) if payload.session_id == "session"
        ));
    }

    #[test]
    fn response_serde_round_trips_all_read_shapes() {
        let workspaces = SessionLogResponse::Workspaces {
            workspaces: vec![WorkspaceSummary {
                directory: "workspace".to_string(),
                session_count: 1,
                last_updated_at: 2,
            }],
        };
        let sessions = SessionLogResponse::Sessions {
            page: Page {
                page: 1,
                page_size: 10,
                total: 11,
            },
            sessions: vec![snapshot_fixture("session")],
        };
        let records = SessionLogResponse::Records {
            page: Page::default(),
            records: vec![SessionRecord {
                session_id: "session".to_string(),
                message_id: "message".to_string(),
                role: "assistant".to_string(),
                created_at: 1,
                updated_at: 2,
                record: json!({ "id": "message" }),
            }],
        };

        for response in [
            SessionLogResponse::Ok,
            SessionLogResponse::SessionCommandApplied {
                result: Box::new(SessionCommandResult {
                    event: SessionEvent::SessionCreated {
                        task_plan: lifecycle::TaskPlan::default(),
                    },
                    projection: SessionAggregate::new("session".to_string())
                        .query(SessionQuery::Lifecycle),
                    session_name: Some("Session".to_string()),
                    message_count: 0,
                    last_user_message_at: None,
                }),
            },
            workspaces,
            sessions,
            SessionLogResponse::Session { session: None },
            records,
            SessionLogResponse::Error {
                error: "boom".to_string(),
            },
        ] {
            let encoded = serde_json::to_string(&response).expect("response json");
            let decoded: SessionLogResponse =
                serde_json::from_str(&encoded).expect("response round trip");
            assert_eq!(
                std::mem::discriminant(&decoded),
                std::mem::discriminant(&response)
            );
        }
    }

    #[test]
    fn session_snapshot_requires_canonical_lifecycle_projection() {
        let mut value =
            serde_json::to_value(snapshot_fixture("session")).expect("canonical snapshot json");
        value
            .as_object_mut()
            .expect("snapshot object")
            .remove("lifecycle_projection");

        let error = serde_json::from_value::<SessionSnapshot>(value)
            .expect_err("snapshot without a canonical lifecycle projection must fail");

        assert!(error.to_string().contains("lifecycle_projection"));
    }

    #[test]
    fn typed_session_mutations_reject_extra_fields() {
        let create = CreateSessionRequest {
            command_id: "create:session".to_string(),
            session_id: "session".to_string(),
            creation_command: SessionCommand::CreateSession {
                task_plan: lifecycle::TaskPlan::default(),
            },
            copy_context: false,
            workspace: "C:/workspace".to_string(),
            session_directory: "C:/workspace".to_string(),
            name: "Session".to_string(),
            created_at: 1,
            model: None,
            agent: None,
            session_type: "coding".to_string(),
            kill_processes_on_start: false,
            validator_enabled: false,
            force_planning: false,
            model_variant: None,
            model_acceleration_enabled: false,
            disable_permission_restrictions: false,
            use_last_tool_call_response: false,
            auto_session_name: true,
            initial_task_plan_patch: None,
        };
        let command = SessionLogCommand::CreateSession(Box::new(create.clone()));
        let command_value = serde_json::to_value(&command).expect("create command json");
        assert_eq!(command_value["command"], "create_session");
        assert_eq!(command_value["session_id"], "session");
        assert!(command_value.get("payload").is_none());
        assert!(matches!(
            serde_json::from_value::<SessionLogCommand>(command_value)
                .expect("create command round trip"),
            SessionLogCommand::CreateSession(payload) if payload.session_id == "session"
        ));

        let value = serde_json::to_value(create).expect("create json");
        assert!(serde_json::from_value::<CreateSessionRequest>(value).is_ok());
        assert!(
            serde_json::from_value::<CreateSessionRequest>(json!({
                "session_id": "session",
                "creation_command": {
                    "command": "create_session",
                    "task_plan": lifecycle::TaskPlan::default()
                },
                "copy_context": false,
                "workspace": "C:/workspace",
                "session_directory": "C:/workspace",
                "name": "Session",
                "created_at": 1,
                "model": null,
                "agent": null,
                "session_type": "coding",
                "kill_processes_on_start": false,
                "validator_enabled": false,
                "force_planning": false,
                "model_variant": null,
                "model_acceleration_enabled": false,
                "disable_permission_restrictions": false,
                "use_last_tool_call_response": false,
                "auto_session_name": true
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<ExecuteSessionCommandRequest>(json!({
                "session_id": "session",
                "session_command": { "command": "submit_user_input" }
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<ExecuteSessionCommandRequest>(json!({
                "command_id": "command",
                "session_id": "session",
                "session_command": { "command": "submit_user_input" },
                "extra": true
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<UpdateSessionRequest>(json!({
                "command_id": "update",
                "session_id": "session",
                "metadata": { "extra": true },
                "task_plan_patch": null
            }))
            .is_err()
        );
        assert!(
            serde_json::from_value::<SessionFeedEvent>(json!({
                "event": "session_snapshot_created",
                "snapshot": {
                    "session_id": "session",
                    "workspace": "C:/workspace",
                    "name": "Session",
                    "parent_id": null,
                    "created_at": 1,
                    "updated_at": 2,
                    "state": "created",
                    "status": "idle",
                    "message_count": 0,
                    "task_management": {},
                    "lifecycle_projection": {
                        "session_id": "session",
                        "state": "created",
                        "parent_id": null,
                        "task_plan": {},
                        "pending_user_inputs": [],
                        "cancelled": false,
                        "runtime_ids": [],
                        "active_runtime_id": null
                    },
                    "management": {},
                    "session": {},
                    "todos": [],
                    "extra": true
                }
            }))
            .is_err()
        );
    }
}
