use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fmt;
use std::path::PathBuf;

use crate::RuntimeId;

pub type SessionId = String;

pub const TASK_SCHEDULING_CONTRACT_SCHEMA_VERSION: &str = "tura_task_scheduling_contract_v1";
pub const MAX_TASK_SCHEDULING_DEPENDENCIES: usize = 64;
pub const MAX_TASK_SCHEDULING_SCOPE_ENTRIES: usize = 1_024;
pub const MAX_TASK_SCHEDULING_INPUTS: usize = 128;
pub const MAX_TASK_SCHEDULING_CONFLICT_IDENTITIES: usize = 128;
const MAX_TASK_SCHEDULING_VALUE_CHARS: usize = 1_024;
const MAX_TASK_SCHEDULING_PARALLEL_RUNTIME_WORKERS: usize = 128;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TaskSchedulingContractV1 {
    pub schema_version: String,
    pub mission_id: String,
    pub semantic_dispatch_key: String,
    pub authority_mission_revision_sha256: String,
    pub delegated_input_sha256: String,
    pub task_context_capsule_semantic_sha256: String,
    pub dependency_task_ids: Vec<String>,
    pub exact_input_sha256s: Vec<String>,
    pub jspace_authorization_semantic_sha256: String,
    pub read_scopes: Vec<String>,
    pub write_scopes: Vec<String>,
    pub declared_targets: Vec<String>,
    pub conflict_identities: Vec<String>,
    pub maximum_parallel_runtime_workers: usize,
}

pub const TASK_DISPATCH_CLAIM_SCHEMA_VERSION: &str = "tura_task_dispatch_claim_v1";
pub const ACKNOWLEDGED_CHILD_CALLBACK_IDENTITY_SCHEMA_VERSION: &str =
    "tura_acknowledged_child_callback_identity_v1";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TaskDispatchClaimV1 {
    pub schema_version: String,
    pub mission_id: String,
    pub task_id: String,
    pub child_session_id: String,
    pub child_runtime_id: String,
    pub child_lease_id: String,
    pub child_transaction_id: String,
    pub semantic_dispatch_key: String,
    pub authority_mission_revision_sha256: String,
    pub delegated_input_sha256: String,
    pub task_context_capsule_semantic_sha256: String,
    pub parent_task_plan_sha256: String,
    pub task_scheduling_contract_sha256: String,
    pub scope_claim_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AcknowledgedChildCallbackIdentityV1 {
    pub schema_version: String,
    pub parent_mission_revision_sha256: String,
    pub commander_thread_id: String,
    pub child_session_id: String,
    pub child_runtime_id: String,
    pub child_lease_id: String,
    pub child_transaction_id: String,
    pub event_id: String,
    pub callback_payload_sha256: String,
    pub effect_identity_sha256: String,
}

impl AcknowledgedChildCallbackIdentityV1 {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.schema_version != ACKNOWLEDGED_CHILD_CALLBACK_IDENTITY_SCHEMA_VERSION {
            return Err("ACKNOWLEDGED_CHILD_CALLBACK_IDENTITY_SCHEMA_UNSUPPORTED");
        }
        for value in [
            &self.commander_thread_id,
            &self.child_session_id,
            &self.child_runtime_id,
            &self.child_lease_id,
            &self.child_transaction_id,
            &self.event_id,
        ] {
            if value.trim().is_empty()
                || value.trim() != value
                || value.len() > MAX_TASK_SCHEDULING_VALUE_CHARS
            {
                return Err("ACKNOWLEDGED_CHILD_CALLBACK_IDENTITY_INVALID");
            }
        }
        for value in [
            &self.parent_mission_revision_sha256,
            &self.callback_payload_sha256,
            &self.effect_identity_sha256,
        ] {
            if !is_lower_sha256(value) {
                return Err("ACKNOWLEDGED_CHILD_CALLBACK_DIGEST_INVALID");
            }
        }
        Ok(())
    }

    pub fn receipt_command_id(&self, parent_session_id: &str) -> String {
        let identity = serde_json::json!({
            "command": "converge_acknowledged_child_callback",
            "parent_session_id": parent_session_id,
            "acknowledgment": self,
        });
        format!(
            "acknowledged-child-callback:{}",
            canonical_value_sha256(&identity)
        )
    }

    fn matches_dispatch_claim(&self, claim: &TaskDispatchClaimV1) -> bool {
        claim.validate().is_ok()
            && claim.child_session_id == self.child_session_id
            && claim.child_runtime_id == self.child_runtime_id
            && claim.child_lease_id == self.child_lease_id
            && claim.child_transaction_id == self.child_transaction_id
            && claim.authority_mission_revision_sha256 == self.parent_mission_revision_sha256
    }
}

impl TaskDispatchClaimV1 {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.schema_version != TASK_DISPATCH_CLAIM_SCHEMA_VERSION {
            return Err("TASK_DISPATCH_CLAIM_SCHEMA_UNSUPPORTED");
        }
        for value in [
            &self.mission_id,
            &self.task_id,
            &self.child_session_id,
            &self.child_runtime_id,
            &self.child_lease_id,
            &self.child_transaction_id,
        ] {
            if value.trim().is_empty()
                || value.trim() != value
                || value.len() > MAX_TASK_SCHEDULING_VALUE_CHARS
            {
                return Err("TASK_DISPATCH_CLAIM_IDENTITY_INVALID");
            }
        }
        for value in [
            &self.semantic_dispatch_key,
            &self.authority_mission_revision_sha256,
            &self.delegated_input_sha256,
            &self.task_context_capsule_semantic_sha256,
            &self.parent_task_plan_sha256,
            &self.task_scheduling_contract_sha256,
            &self.scope_claim_sha256,
        ] {
            if !is_lower_sha256(value) {
                return Err("TASK_DISPATCH_CLAIM_DIGEST_INVALID");
            }
        }
        Ok(())
    }
}

impl TaskSchedulingContractV1 {
    pub fn validate(&self, task_id: &str) -> Result<(), &'static str> {
        if self.schema_version != TASK_SCHEDULING_CONTRACT_SCHEMA_VERSION {
            return Err("TASK_SCHEDULING_SCHEMA_UNSUPPORTED");
        }
        if !is_lower_sha256(&self.semantic_dispatch_key) {
            return Err("TASK_SCHEDULING_SEMANTIC_DISPATCH_KEY_INVALID");
        }
        if self.mission_id.trim().is_empty()
            || self.mission_id.len() > MAX_TASK_SCHEDULING_VALUE_CHARS
        {
            return Err("TASK_SCHEDULING_MISSION_ID_INVALID");
        }
        if !is_lower_sha256(&self.authority_mission_revision_sha256) {
            return Err("TASK_SCHEDULING_AUTHORITY_REVISION_INVALID");
        }
        if !is_lower_sha256(&self.jspace_authorization_semantic_sha256) {
            return Err("TASK_SCHEDULING_JSPACE_IDENTITY_INVALID");
        }
        if !is_lower_sha256(&self.delegated_input_sha256) {
            return Err("TASK_SCHEDULING_DELEGATED_INPUT_INVALID");
        }
        if !is_lower_sha256(&self.task_context_capsule_semantic_sha256) {
            return Err("TASK_SCHEDULING_CAPSULE_IDENTITY_INVALID");
        }
        if self.dependency_task_ids.len() > MAX_TASK_SCHEDULING_DEPENDENCIES
            || !canonical_values(&self.dependency_task_ids)
            || self
                .dependency_task_ids
                .iter()
                .any(|dependency| dependency == task_id)
        {
            return Err("TASK_SCHEDULING_DEPENDENCIES_INVALID");
        }
        if self.exact_input_sha256s.len() > MAX_TASK_SCHEDULING_INPUTS
            || !canonical_values(&self.exact_input_sha256s)
            || self
                .exact_input_sha256s
                .iter()
                .any(|value| !is_lower_sha256(value))
        {
            return Err("TASK_SCHEDULING_EXACT_INPUTS_INVALID");
        }
        for scopes in [
            &self.read_scopes,
            &self.write_scopes,
            &self.declared_targets,
        ] {
            if scopes.len() > MAX_TASK_SCHEDULING_SCOPE_ENTRIES || !canonical_values(scopes) {
                return Err("TASK_SCHEDULING_SCOPE_INVALID");
            }
        }
        if self.conflict_identities.len() > MAX_TASK_SCHEDULING_CONFLICT_IDENTITIES
            || !canonical_values(&self.conflict_identities)
        {
            return Err("TASK_SCHEDULING_CONFLICT_IDENTITIES_INVALID");
        }
        if self.maximum_parallel_runtime_workers == 0
            || self.maximum_parallel_runtime_workers > MAX_TASK_SCHEDULING_PARALLEL_RUNTIME_WORKERS
        {
            return Err("TASK_SCHEDULING_PARALLEL_RUNTIME_LIMIT_INVALID");
        }
        Ok(())
    }

    pub fn semantic_dispatch_identity(&self, task_id: &str) -> serde_json::Value {
        serde_json::json!({
            "schema_version": "tura_task_semantic_dispatch_identity_v1",
            "contract_schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "authority_mission_revision_sha256": self.authority_mission_revision_sha256,
            "task_id": task_id,
            "delegated_input_sha256": self.delegated_input_sha256,
            "task_context_capsule_semantic_sha256": self.task_context_capsule_semantic_sha256,
            "dependency_task_ids": self.dependency_task_ids,
            "exact_input_sha256s": self.exact_input_sha256s,
            "jspace_authorization_semantic_sha256": self.jspace_authorization_semantic_sha256,
            "read_scopes": self.read_scopes,
            "write_scopes": self.write_scopes,
            "declared_targets": self.declared_targets,
            "conflict_identities": self.conflict_identities,
            "maximum_parallel_runtime_workers": self.maximum_parallel_runtime_workers,
        })
    }

    pub fn semantic_dispatch_sha256(&self, task_id: &str) -> String {
        canonical_value_sha256(&self.semantic_dispatch_identity(task_id))
    }

    pub fn contract_sha256(&self) -> String {
        canonical_value_sha256(
            &serde_json::to_value(self).expect("task scheduling contract is serializable"),
        )
    }

    pub fn scope_claim_sha256(&self) -> String {
        canonical_value_sha256(&serde_json::json!({
            "jspace_authorization_semantic_sha256": self.jspace_authorization_semantic_sha256,
            "read_scopes": self.read_scopes,
            "write_scopes": self.write_scopes,
            "declared_targets": self.declared_targets,
            "conflict_identities": self.conflict_identities,
        }))
    }
}

pub fn task_plan_ready_set_sha256(task_plan: &TaskPlan) -> String {
    let tasks = task_plan
        .detailed_tasks
        .iter()
        .map(|task| serde_json::to_value(task).expect("task step is serializable"))
        .collect::<Vec<_>>();
    canonical_value_sha256(&serde_json::json!({
        "schema_version": "tura_task_plan_ready_set_identity_v1",
        "plan_summary": task_plan.plan_summary,
        "tasks": tasks,
    }))
}

pub fn canonical_value_sha256(value: &Value) -> String {
    fn canonical(value: &Value) -> String {
        match value {
            Value::Null => "null".to_string(),
            Value::Bool(value) => value.to_string(),
            Value::Number(value) => value.to_string(),
            Value::String(value) => serde_json::to_string(value).expect("JSON string is encodable"),
            Value::Array(values) => format!(
                "[{}]",
                values.iter().map(canonical).collect::<Vec<_>>().join(",")
            ),
            Value::Object(values) => {
                let mut keys = values.keys().collect::<Vec<_>>();
                keys.sort();
                let fields = keys
                    .into_iter()
                    .map(|key| {
                        format!(
                            "{}:{}",
                            serde_json::to_string(key).expect("JSON key is encodable"),
                            canonical(&values[key])
                        )
                    })
                    .collect::<Vec<_>>();
                format!("{{{}}}", fields.join(","))
            }
        }
    }
    format!("{:x}", Sha256::digest(canonical(value).as_bytes()))
}

fn canonical_values(values: &[String]) -> bool {
    values.iter().all(|value| {
        !value.trim().is_empty()
            && value.len() <= MAX_TASK_SCHEDULING_VALUE_CHARS
            && value.trim() == value
    }) && values.windows(2).all(|pair| pair[0] < pair[1])
}

fn is_lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskReadySetState {
    Ready,
    BlockedState,
    BlockedDependency,
    BlockedScope,
    BlockedLease,
    BlockedCapacity,
    TypedUnknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TaskReadySetDecision {
    pub state: TaskReadySetState,
    pub reason_codes: Vec<String>,
    pub blocking_task_ids: Vec<String>,
}

impl TaskReadySetDecision {
    fn new(
        state: TaskReadySetState,
        reason: impl Into<String>,
        blocking_task_ids: impl IntoIterator<Item = String>,
    ) -> Self {
        let mut blocking_task_ids = blocking_task_ids.into_iter().collect::<Vec<_>>();
        blocking_task_ids.sort();
        blocking_task_ids.dedup();
        Self {
            state,
            reason_codes: vec![reason.into()],
            blocking_task_ids,
        }
    }

    pub fn typed_unknown(reason: impl Into<String>) -> Self {
        Self::new(TaskReadySetState::TypedUnknown, reason, [])
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaskLeaseReadinessEvidence {
    pub task_id: String,
    pub lease_id: String,
    pub lease_active: bool,
    pub terminal: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaskReadySetEvidence {
    pub evaluated_at_ms: i64,
    pub parent_session_can_transition_to_running: bool,
    pub authority_mission_revision_sha256: String,
    pub execution_terminal_task_ids: BTreeSet<String>,
    pub available_exact_input_sha256s: Option<BTreeSet<String>>,
    pub semantic_dispatch_identity_valid: bool,
    pub candidate_scope_identity_valid: bool,
    pub parallel_runtime_limit_supported: bool,
    pub task_leases: Vec<TaskLeaseReadinessEvidence>,
    pub lease_unknown_task_ids: BTreeSet<String>,
    pub scope_evaluated_task_ids: BTreeSet<String>,
    pub scope_conflicting_task_ids: BTreeSet<String>,
    pub scope_unknown_task_ids: BTreeSet<String>,
    pub active_runtime_workers: usize,
    pub requested_lease_id: Option<String>,
    pub claimed_task_id: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum PlanStatus {
    #[default]
    Todo,
    WaitingUser,
    Doing,
    Question,
    Done,
    Archived,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum StartCondition {
    SessionIdle,
    #[default]
    UserAction,
    ScheduledTask,
    PollingTask,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct PollInterval {
    #[serde(default)]
    pub m: u64,
    #[serde(default)]
    pub d: u64,
    #[serde(default)]
    pub h: u64,
    #[serde(default)]
    pub s: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TaskStep {
    #[serde(default)]
    pub task_id: String,
    #[serde(default)]
    pub step: u64,
    #[serde(default)]
    pub sub_session_id: String,
    #[serde(default = "Utc::now")]
    pub start_at: DateTime<Utc>,
    #[serde(default)]
    pub poll_interval: PollInterval,
    #[serde(default)]
    pub start_condition: StartCondition,
    #[serde(default)]
    pub status: PlanStatus,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scheduling_contract: Option<TaskSchedulingContractV1>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dispatch_claim: Option<TaskDispatchClaimV1>,
    #[serde(default)]
    pub task_summary: String,
    #[serde(default)]
    pub step_task: String,
    #[serde(default)]
    pub step_turn: u64,
    #[serde(default)]
    pub step_tool: String,
    #[serde(default)]
    pub step_context: String,
    #[serde(default)]
    pub step_agent_name: String,
    #[serde(default)]
    pub step_deliverable_description: String,
    #[serde(default)]
    pub step_deliverable_path: PathBuf,
}

impl Default for TaskStep {
    fn default() -> Self {
        Self {
            task_id: String::new(),
            step: 0,
            sub_session_id: String::new(),
            start_at: Utc::now(),
            poll_interval: PollInterval::default(),
            start_condition: StartCondition::default(),
            status: PlanStatus::default(),
            scheduling_contract: None,
            dispatch_claim: None,
            task_summary: String::new(),
            step_task: String::new(),
            step_turn: 0,
            step_tool: String::new(),
            step_context: String::new(),
            step_agent_name: String::new(),
            step_deliverable_description: String::new(),
            step_deliverable_path: PathBuf::new(),
        }
    }
}

impl TaskStep {
    pub fn scheduler_eligible(&self, now: DateTime<Utc>) -> bool {
        if matches!(
            self.status,
            PlanStatus::WaitingUser | PlanStatus::Done | PlanStatus::Archived
        ) {
            return false;
        }
        match self.start_condition {
            StartCondition::ScheduledTask | StartCondition::PollingTask => {
                matches!(self.status, PlanStatus::Todo | PlanStatus::Question)
                    && self.start_at <= now
            }
            StartCondition::SessionIdle => {
                matches!(self.status, PlanStatus::Todo | PlanStatus::Question)
            }
            StartCondition::UserAction => false,
        }
    }

    fn direct_dispatch_eligible(&self, now: DateTime<Utc>) -> bool {
        match self.start_condition {
            StartCondition::UserAction => {
                matches!(self.status, PlanStatus::Todo | PlanStatus::Question)
            }
            _ => self.scheduler_eligible(now),
        }
    }

    pub fn display_summary(&self, plan_summary: &str) -> String {
        [
            self.task_summary.as_str(),
            self.step_task.as_str(),
            plan_summary,
        ]
        .into_iter()
        .map(str::trim)
        .find(|value| !value.is_empty())
        .unwrap_or("Continue planned task")
        .to_string()
    }

    pub fn advance_polling_start(&mut self, now: DateTime<Utc>) {
        let seconds = self
            .poll_interval
            .s
            .saturating_add(self.poll_interval.m.saturating_mul(60))
            .saturating_add(self.poll_interval.h.saturating_mul(60 * 60))
            .saturating_add(self.poll_interval.d.saturating_mul(24 * 60 * 60))
            .max(1);
        let step = Duration::seconds(seconds.min(i64::MAX as u64) as i64);
        let mut next = self.start_at + step;
        while next <= now {
            next += step;
        }
        self.start_at = next;
    }
}

pub fn classify_task_ready_set(
    task_plan: &TaskPlan,
    task_id: &str,
    evidence: &TaskReadySetEvidence,
) -> TaskReadySetDecision {
    if !task_plan.scheduling_identities_valid() {
        return TaskReadySetDecision::new(
            TaskReadySetState::TypedUnknown,
            "TASK_READY_SET_TASK_IDENTITIES_INVALID",
            [],
        );
    }
    let Some(task) = task_plan
        .detailed_tasks
        .iter()
        .find(|candidate| candidate.task_id == task_id)
    else {
        return TaskReadySetDecision::new(
            TaskReadySetState::TypedUnknown,
            "TASK_READY_SET_TASK_NOT_FOUND",
            [],
        );
    };
    let Some(contract) = task.scheduling_contract.as_ref() else {
        return TaskReadySetDecision::new(
            TaskReadySetState::TypedUnknown,
            "TASK_READY_SET_SCHEDULING_CONTRACT_MISSING",
            [],
        );
    };
    if let Err(reason) = contract.validate(task_id) {
        return TaskReadySetDecision::new(TaskReadySetState::TypedUnknown, reason, []);
    }
    if contract.authority_mission_revision_sha256 != evidence.authority_mission_revision_sha256 {
        return TaskReadySetDecision::new(
            TaskReadySetState::TypedUnknown,
            "TASK_READY_SET_AUTHORITY_REVISION_MISMATCH",
            [],
        );
    }
    if !evidence.semantic_dispatch_identity_valid {
        return TaskReadySetDecision::new(
            TaskReadySetState::TypedUnknown,
            "TASK_READY_SET_SEMANTIC_DISPATCH_IDENTITY_MISMATCH",
            [],
        );
    }
    if !evidence.candidate_scope_identity_valid {
        return TaskReadySetDecision::new(
            TaskReadySetState::TypedUnknown,
            "TASK_READY_SET_SCOPE_IDENTITY_INVALID",
            [],
        );
    }
    if !evidence.parallel_runtime_limit_supported {
        return TaskReadySetDecision::new(
            TaskReadySetState::TypedUnknown,
            "TASK_READY_SET_PARALLEL_RUNTIME_LIMIT_UNSUPPORTED",
            [],
        );
    }
    if !evidence.parent_session_can_transition_to_running {
        return TaskReadySetDecision::new(
            TaskReadySetState::BlockedState,
            "TASK_READY_SET_PARENT_SESSION_TERMINAL",
            [],
        );
    }
    match evidence.available_exact_input_sha256s.as_ref() {
        Some(available)
            if available
                == &contract
                    .exact_input_sha256s
                    .iter()
                    .cloned()
                    .collect::<BTreeSet<_>>() => {}
        Some(_) => {
            return TaskReadySetDecision::new(
                TaskReadySetState::TypedUnknown,
                "TASK_READY_SET_EXACT_INPUT_IDENTITY_MISSING",
                [],
            );
        }
        None if contract.exact_input_sha256s.is_empty() => {}
        None => {
            return TaskReadySetDecision::new(
                TaskReadySetState::TypedUnknown,
                "TASK_READY_SET_EXACT_INPUT_AVAILABILITY_UNKNOWN",
                [],
            );
        }
    }
    let claimed_by_this_admission =
        task.status == PlanStatus::Doing && evidence.claimed_task_id.as_deref() == Some(task_id);
    let direct_dispatch_eligible = task.start_condition == StartCondition::UserAction
        && evidence.requested_lease_id.is_some()
        && matches!(task.status, PlanStatus::Todo | PlanStatus::Question);
    let execution_eligible = claimed_by_this_admission
        || direct_dispatch_eligible
        || match task.start_condition {
            StartCondition::ScheduledTask | StartCondition::PollingTask => {
                matches!(task.status, PlanStatus::Todo | PlanStatus::Question)
                    && task.start_at.timestamp_millis() <= evidence.evaluated_at_ms
            }
            StartCondition::SessionIdle => {
                matches!(task.status, PlanStatus::Todo | PlanStatus::Question)
            }
            StartCondition::UserAction => false,
        };
    if !execution_eligible {
        let reason = match (task.status, task.start_condition) {
            (PlanStatus::Done | PlanStatus::Archived, _) => "TASK_READY_SET_TASK_TERMINAL",
            (PlanStatus::WaitingUser, _) => "TASK_READY_SET_WAITING_USER",
            (PlanStatus::Doing, _) => "TASK_READY_SET_TASK_ALREADY_RUNNING",
            (_, StartCondition::UserAction) => "TASK_READY_SET_USER_ACTION_REQUIRED",
            (_, StartCondition::ScheduledTask | StartCondition::PollingTask)
                if task.start_at.timestamp_millis() > evidence.evaluated_at_ms =>
            {
                "TASK_READY_SET_NOT_DUE"
            }
            _ => "TASK_READY_SET_TASK_STATE_BLOCKED",
        };
        return TaskReadySetDecision::new(TaskReadySetState::BlockedState, reason, []);
    }

    for dependency_id in &contract.dependency_task_ids {
        let Some(dependency) = task_plan
            .detailed_tasks
            .iter()
            .find(|candidate| &candidate.task_id == dependency_id)
        else {
            return TaskReadySetDecision::new(
                TaskReadySetState::TypedUnknown,
                "TASK_READY_SET_DEPENDENCY_NOT_FOUND",
                [dependency_id.clone()],
            );
        };
        let execution_terminal =
            matches!(dependency.status, PlanStatus::Done | PlanStatus::Archived)
                || evidence.execution_terminal_task_ids.contains(dependency_id);
        if !execution_terminal {
            return TaskReadySetDecision::new(
                TaskReadySetState::BlockedDependency,
                "TASK_READY_SET_DEPENDENCY_NOT_TERMINAL",
                [dependency_id.clone()],
            );
        }
    }

    if evidence.lease_unknown_task_ids.contains(task_id) {
        return TaskReadySetDecision::new(
            TaskReadySetState::TypedUnknown,
            "TASK_READY_SET_LEASE_EVIDENCE_UNKNOWN",
            [task_id.to_string()],
        );
    }
    if let Some(unknown) = evidence
        .lease_unknown_task_ids
        .iter()
        .find(|unknown| unknown.as_str() != task_id)
    {
        return TaskReadySetDecision::new(
            TaskReadySetState::TypedUnknown,
            "TASK_READY_SET_ACTIVE_LEASE_SET_UNKNOWN",
            [unknown.clone()],
        );
    }

    if let Some(active) = evidence.task_leases.iter().find(|lease| {
        lease.task_id == task_id
            && lease.lease_active
            && !lease.terminal
            && evidence.requested_lease_id.as_deref() != Some(lease.lease_id.as_str())
    }) {
        return TaskReadySetDecision::new(
            TaskReadySetState::BlockedLease,
            "TASK_READY_SET_LEASE_CONFLICT",
            [active.task_id.clone()],
        );
    }
    if evidence.active_runtime_workers >= contract.maximum_parallel_runtime_workers {
        return TaskReadySetDecision::new(
            TaskReadySetState::BlockedCapacity,
            "TASK_READY_SET_CAPACITY_EXHAUSTED",
            [],
        );
    }

    for lease in evidence
        .task_leases
        .iter()
        .filter(|lease| lease.task_id != task_id && lease.lease_active && !lease.terminal)
    {
        if evidence.scope_unknown_task_ids.contains(&lease.task_id)
            || !evidence.scope_evaluated_task_ids.contains(&lease.task_id)
        {
            return TaskReadySetDecision::new(
                TaskReadySetState::TypedUnknown,
                "TASK_READY_SET_ACTIVE_SCOPE_UNKNOWN",
                [lease.task_id.clone()],
            );
        }
        if evidence.scope_conflicting_task_ids.contains(&lease.task_id) {
            return TaskReadySetDecision::new(
                TaskReadySetState::BlockedScope,
                "TASK_READY_SET_SCOPE_CONFLICT",
                [lease.task_id.clone()],
            );
        }
    }

    TaskReadySetDecision::new(TaskReadySetState::Ready, "TASK_READY_SET_READY", [])
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Default)]
#[serde(deny_unknown_fields)]
pub struct TaskPlan {
    #[serde(default)]
    pub plan_summary: String,
    #[serde(default)]
    pub detailed_tasks: Vec<TaskStep>,
}

impl<'de> Deserialize<'de> for TaskPlan {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct TaskPlanVisitor;

        impl<'de> serde::de::Visitor<'de> for TaskPlanVisitor {
            type Value = TaskPlan;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a task plan object")
            }

            fn visit_map<A>(self, map: A) -> Result<Self::Value, A::Error>
            where
                A: serde::de::MapAccess<'de>,
            {
                #[derive(Deserialize)]
                #[serde(deny_unknown_fields)]
                struct Wire {
                    #[serde(default)]
                    plan_summary: String,
                    #[serde(default)]
                    detailed_tasks: Vec<TaskStep>,
                }

                let wire = Wire::deserialize(serde::de::value::MapAccessDeserializer::new(map))?;
                Ok(TaskPlan {
                    plan_summary: wire.plan_summary,
                    detailed_tasks: wire.detailed_tasks,
                })
            }
        }

        deserializer.deserialize_map(TaskPlanVisitor)
    }
}

impl TaskPlan {
    pub fn scheduling_identities_valid(&self) -> bool {
        let mut identities = BTreeSet::new();
        self.detailed_tasks.iter().all(|task| {
            !task.task_id.trim().is_empty()
                && task.task_id.trim() == task.task_id
                && identities.insert(task.task_id.as_str())
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct SessionTaskPatch {
    pub task_id: Option<String>,
    pub step: Option<u64>,
    pub task_summary: Option<String>,
    pub deliverable: Option<String>,
    pub sub_session_id: Option<String>,
    pub start_condition: Option<StartCondition>,
    pub start_at: Option<DateTime<Utc>>,
    pub poll_interval: Option<PollInterval>,
    pub status: Option<PlanStatus>,
    pub scheduling_contract: Option<TaskSchedulingContractV1>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SessionTaskPlanPatch {
    pub plan_summary: Option<String>,
    pub tasks: Option<Vec<SessionTaskPatch>>,
    pub task: Option<SessionTaskPatch>,
    pub generated_task_ids: Vec<String>,
    pub generated_task_id: String,
    pub now: DateTime<Utc>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SessionState {
    Created,
    Running,
    Paused,
    Completed,
    Failed,
    Cancelled,
    Interrupted,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SessionAggregate {
    pub session_id: SessionId,
    pub state: SessionState,
    pub parent_id: Option<SessionId>,
    pub task_plan: TaskPlan,
    pub pending_user_inputs: Vec<String>,
    pub cancelled: bool,
    pub runtime_ids: Vec<RuntimeId>,
    pub active_runtime_id: Option<RuntimeId>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
pub enum SessionCommand {
    CreateSession {
        task_plan: TaskPlan,
    },
    SubmitUserInput,
    StartUserTurn,
    QueueUserInputWhileBusy {
        input: String,
    },
    ConsumeQueuedUserInputs,
    RuntimeStarted {
        runtime_id: RuntimeId,
    },
    RuntimeRetried {
        runtime_id: RuntimeId,
        fallback_from_id: RuntimeId,
    },
    RuntimeCompleted {
        runtime_id: RuntimeId,
    },
    RuntimeFailed {
        runtime_id: RuntimeId,
    },
    RuntimeCancelled {
        runtime_id: RuntimeId,
    },
    RuntimeEnded {
        runtime_id: RuntimeId,
    },
    ApplyRuntimeState {
        state: SessionState,
    },
    InterruptSession,
    CancelSession,
    RegisterChildSession {
        parent_id: SessionId,
    },
    ForkSession {
        parent_id: SessionId,
    },
    ApplyTaskStatus {
        task_plan: TaskPlan,
    },
    ApplyTaskPatch {
        patch: SessionTaskPatch,
        generated_task_id: String,
        now: DateTime<Utc>,
    },
    ApplyTaskPatches {
        tasks: Vec<SessionTaskPatch>,
        generated_task_ids: Vec<String>,
        now: DateTime<Utc>,
    },
    ApplyTaskPlanPatch {
        patch: SessionTaskPlanPatch,
    },
    StartScheduledTask {
        task_id: String,
        task_summary: String,
        start_condition: StartCondition,
        now: DateTime<Utc>,
    },
    ClaimScheduledChildTask {
        expected_task_plan: TaskPlan,
        claim: TaskDispatchClaimV1,
        now: DateTime<Utc>,
    },
    ConvergeAcknowledgedChildCallback {
        acknowledgment: AcknowledgedChildCallbackIdentityV1,
    },
    DeleteSession,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case", deny_unknown_fields)]
pub enum SessionEvent {
    SessionCreated {
        task_plan: TaskPlan,
    },
    UserInputAccepted {
        state: SessionState,
    },
    UserTurnStarted {
        state: SessionState,
    },
    UserInputQueued {
        input: String,
    },
    QueuedUserInputsConsumed {
        inputs: Vec<String>,
    },
    RuntimeStarted {
        runtime_id: RuntimeId,
        state: SessionState,
    },
    RuntimeCompleted {
        runtime_id: RuntimeId,
        state: SessionState,
    },
    RuntimeFailed {
        runtime_id: RuntimeId,
        state: SessionState,
    },
    RuntimeCancelled {
        runtime_id: RuntimeId,
        state: SessionState,
    },
    RuntimeEnded {
        runtime_id: RuntimeId,
        state: SessionState,
    },
    RuntimeStateApplied {
        state: SessionState,
    },
    SessionInterrupted {
        state: SessionState,
        task_plan: TaskPlan,
    },
    SessionCancelled {
        state: SessionState,
    },
    ChildSessionRegistered {
        parent_id: SessionId,
        state: SessionState,
    },
    SessionForked {
        parent_id: SessionId,
    },
    TaskPlanChanged {
        task_plan: TaskPlan,
    },
    ScheduledTaskClaimed {
        task_plan: TaskPlan,
        task_id: String,
        task_summary: String,
        start_condition: StartCondition,
        state: SessionState,
    },
    ScheduledChildTaskClaimed {
        task_plan: TaskPlan,
        claim: TaskDispatchClaimV1,
        claimed_at: DateTime<Utc>,
        state: SessionState,
    },
    AcknowledgedChildCallbackConverged {
        acknowledgment: AcknowledgedChildCallbackIdentityV1,
        task_plan: TaskPlan,
        state: SessionState,
    },
    SessionDeleted,
}

impl SessionEvent {
    fn as_command(&self, aggregate: &SessionAggregate) -> Result<Option<SessionCommand>, String> {
        let command = match self {
            Self::SessionCreated { .. } | Self::SessionForked { .. } => return Ok(None),
            Self::UserInputAccepted { .. } => SessionCommand::SubmitUserInput,
            Self::UserTurnStarted { .. } => SessionCommand::StartUserTurn,
            Self::UserInputQueued { input } => SessionCommand::QueueUserInputWhileBusy {
                input: input.clone(),
            },
            Self::QueuedUserInputsConsumed { .. } => SessionCommand::ConsumeQueuedUserInputs,
            Self::RuntimeStarted { runtime_id, .. } => {
                let started = SessionCommand::RuntimeStarted {
                    runtime_id: runtime_id.clone(),
                };
                if aggregate
                    .decide(started.clone())
                    .is_ok_and(|expected| expected == *self)
                {
                    started
                } else if let Some(fallback_from_id) = aggregate.runtime_ids.last() {
                    SessionCommand::RuntimeRetried {
                        runtime_id: runtime_id.clone(),
                        fallback_from_id: fallback_from_id.clone(),
                    }
                } else {
                    started
                }
            }
            Self::RuntimeCompleted { runtime_id, .. } => SessionCommand::RuntimeCompleted {
                runtime_id: runtime_id.clone(),
            },
            Self::RuntimeFailed { runtime_id, .. } => SessionCommand::RuntimeFailed {
                runtime_id: runtime_id.clone(),
            },
            Self::RuntimeCancelled { runtime_id, .. } => SessionCommand::RuntimeCancelled {
                runtime_id: runtime_id.clone(),
            },
            Self::RuntimeEnded { runtime_id, .. } => SessionCommand::RuntimeEnded {
                runtime_id: runtime_id.clone(),
            },
            Self::RuntimeStateApplied { state } => {
                SessionCommand::ApplyRuntimeState { state: *state }
            }
            Self::SessionInterrupted { .. } => SessionCommand::InterruptSession,
            Self::SessionCancelled { .. } => SessionCommand::CancelSession,
            Self::ChildSessionRegistered { parent_id, .. } => {
                SessionCommand::RegisterChildSession {
                    parent_id: parent_id.clone(),
                }
            }
            Self::TaskPlanChanged { task_plan } => SessionCommand::ApplyTaskStatus {
                task_plan: task_plan.clone(),
            },
            Self::ScheduledTaskClaimed { .. } => {
                return aggregate
                    .scheduled_replay_command(self)
                    .map(Some)
                    .ok_or_else(|| {
                        "scheduled_task_claimed does not match a canonical scheduler result"
                            .to_string()
                    });
            }
            Self::ScheduledChildTaskClaimed {
                claim, claimed_at, ..
            } => SessionCommand::ClaimScheduledChildTask {
                expected_task_plan: aggregate.task_plan.clone(),
                claim: claim.clone(),
                now: *claimed_at,
            },
            Self::AcknowledgedChildCallbackConverged { acknowledgment, .. } => {
                SessionCommand::ConvergeAcknowledgedChildCallback {
                    acknowledgment: acknowledgment.clone(),
                }
            }
            Self::SessionDeleted => SessionCommand::DeleteSession,
        };
        Ok(Some(command))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "query", rename_all = "snake_case", deny_unknown_fields)]
pub enum SessionQuery {
    Lifecycle,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SessionProjection {
    pub session_id: SessionId,
    pub state: SessionState,
    pub parent_id: Option<SessionId>,
    pub task_plan: TaskPlan,
    pub pending_user_inputs: Vec<String>,
    pub cancelled: bool,
    pub runtime_ids: Vec<RuntimeId>,
    pub active_runtime_id: Option<RuntimeId>,
}

impl SessionProjection {
    pub fn task_management_json(&self, session_started_at: DateTime<Utc>) -> serde_json::Value {
        crate::session_projection::task_management_json(&self.task_plan, session_started_at)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SessionTransitionError {
    pub previous: SessionState,
    pub next: SessionState,
}

impl fmt::Display for SessionTransitionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "invalid session state transition: {:?} -> {:?}",
            self.previous, self.next
        )
    }
}

impl std::error::Error for SessionTransitionError {}

impl SessionAggregate {
    pub fn new(session_id: SessionId) -> Self {
        Self {
            session_id,
            state: SessionState::Created,
            parent_id: None,
            task_plan: TaskPlan::default(),
            pending_user_inputs: Vec::new(),
            cancelled: false,
            runtime_ids: Vec::new(),
            active_runtime_id: None,
        }
    }

    pub fn execute(
        &mut self,
        command: SessionCommand,
    ) -> Result<SessionEvent, SessionTransitionError> {
        let event = self.decide(command)?;
        self.apply(&event);
        Ok(event)
    }

    pub fn validate_acknowledged_child_callback(
        &self,
        acknowledgment: &AcknowledgedChildCallbackIdentityV1,
    ) -> Result<(), &'static str> {
        self.acknowledged_child_callback_task_index(acknowledgment)
            .map(|_| ())
    }

    fn acknowledged_child_callback_task_index(
        &self,
        acknowledgment: &AcknowledgedChildCallbackIdentityV1,
    ) -> Result<usize, &'static str> {
        acknowledgment.validate()?;
        let matching_indexes = self
            .task_plan
            .detailed_tasks
            .iter()
            .enumerate()
            .filter_map(|(index, task)| {
                task.dispatch_claim
                    .as_ref()
                    .filter(|claim| {
                        claim.task_id == task.task_id
                            && task.sub_session_id == acknowledgment.child_session_id
                            && acknowledgment.matches_dispatch_claim(claim)
                    })
                    .map(|_| index)
            })
            .collect::<Vec<_>>();
        let matching_index = match matching_indexes.as_slice() {
            [matching_index] => *matching_index,
            [] if self.task_plan.detailed_tasks.iter().any(|task| {
                task.sub_session_id == acknowledgment.child_session_id
                    || task.dispatch_claim.as_ref().is_some_and(|claim| {
                        claim.child_session_id == acknowledgment.child_session_id
                    })
            }) =>
            {
                return Err("ACKNOWLEDGED_CHILD_CALLBACK_DISPATCH_CLAIM_MISMATCH");
            }
            [] => return Err("ACKNOWLEDGED_CHILD_CALLBACK_DISPATCH_CLAIM_MISSING"),
            _ => return Err("ACKNOWLEDGED_CHILD_CALLBACK_DISPATCH_CLAIM_AMBIGUOUS"),
        };
        match self.task_plan.detailed_tasks[matching_index].status {
            PlanStatus::Doing | PlanStatus::Done | PlanStatus::Archived => Ok(matching_index),
            PlanStatus::Todo | PlanStatus::WaitingUser | PlanStatus::Question => {
                Err("ACKNOWLEDGED_CHILD_CALLBACK_TASK_STATUS_CONFLICT")
            }
        }
    }

    /// Rebuilds canonical session state from its ordered event stream.
    pub fn replay(
        session_id: SessionId,
        events: impl IntoIterator<Item = SessionEvent>,
    ) -> Result<Self, String> {
        let mut events = events.into_iter();
        let first = events
            .next()
            .ok_or_else(|| format!("session {session_id} has no creation event"))?;
        let creation_command = match &first {
            SessionEvent::SessionCreated { task_plan } => SessionCommand::CreateSession {
                task_plan: task_plan.clone(),
            },
            SessionEvent::ChildSessionRegistered { parent_id, .. } => {
                SessionCommand::RegisterChildSession {
                    parent_id: parent_id.clone(),
                }
            }
            SessionEvent::SessionForked { parent_id } => SessionCommand::ForkSession {
                parent_id: parent_id.clone(),
            },
            _ => return Err("first session event is not a creation event".to_string()),
        };
        let mut aggregate = Self::new(session_id);
        let expected = aggregate
            .decide(creation_command)
            .map_err(|error| error.to_string())?;
        if expected != first {
            return Err(
                "session creation event does not match the canonical reducer result".into(),
            );
        }
        aggregate.apply(&first);
        for event in events {
            aggregate.apply_committed(&event)?;
        }
        Ok(aggregate)
    }

    /// Applies one event received from the canonical ordered stream.
    pub fn apply_committed(&mut self, event: &SessionEvent) -> Result<(), String> {
        if matches!(event, SessionEvent::SessionDeleted) {
            return Err("session_deleted cannot appear in a retained session event stream".into());
        }
        let command = event.as_command(self)?.ok_or_else(|| {
            "session creation events may only be the first session event".to_string()
        })?;
        let expected = self.decide(command).map_err(|error| error.to_string())?;
        if expected != *event {
            return Err("session event does not match the canonical reducer result".to_string());
        }
        self.apply(event);
        Ok(())
    }

    pub fn decide(&self, command: SessionCommand) -> Result<SessionEvent, SessionTransitionError> {
        let previous = self.state;
        match command {
            SessionCommand::CreateSession { task_plan } => {
                Ok(SessionEvent::SessionCreated { task_plan })
            }
            SessionCommand::ApplyRuntimeState { state: next } => {
                let active_runtime_state_transition = self.active_runtime_id.is_some()
                    && matches!(previous, SessionState::Running | SessionState::Paused)
                    && matches!(next, SessionState::Running | SessionState::Paused);
                let execution_terminal_transition = self.active_runtime_id.is_some()
                    && matches!(previous, SessionState::Running | SessionState::Paused)
                    && next.is_terminal();
                if (self.active_runtime_id.is_some()
                    && !active_runtime_state_transition
                    && !execution_terminal_transition)
                    || !previous.can_transition_to(next)
                {
                    return Err(SessionTransitionError { previous, next });
                }
                Ok(SessionEvent::RuntimeStateApplied { state: next })
            }
            SessionCommand::SubmitUserInput => Ok(SessionEvent::UserInputAccepted {
                state: match previous {
                    SessionState::Completed
                    | SessionState::Failed
                    | SessionState::Cancelled
                    | SessionState::Interrupted => SessionState::Created,
                    state => state,
                },
            }),
            SessionCommand::StartUserTurn => {
                if matches!(previous, SessionState::Running | SessionState::Paused) {
                    return Err(SessionTransitionError {
                        previous,
                        next: SessionState::Running,
                    });
                }
                Ok(SessionEvent::UserTurnStarted {
                    state: SessionState::Running,
                })
            }
            SessionCommand::QueueUserInputWhileBusy { input } => {
                let input = input.trim();
                if !matches!(previous, SessionState::Running | SessionState::Paused)
                    || input.is_empty()
                {
                    return Err(SessionTransitionError {
                        previous,
                        next: previous,
                    });
                }
                Ok(SessionEvent::UserInputQueued {
                    input: input.to_string(),
                })
            }
            SessionCommand::ConsumeQueuedUserInputs => Ok(SessionEvent::QueuedUserInputsConsumed {
                inputs: self.pending_user_inputs.clone(),
            }),
            SessionCommand::RuntimeStarted { runtime_id } => {
                let next = SessionState::Running;
                if runtime_id.trim().is_empty()
                    || self
                        .active_runtime_id
                        .as_ref()
                        .is_some_and(|active| active != &runtime_id)
                    || !previous.can_transition_to(next)
                {
                    return Err(SessionTransitionError { previous, next });
                }
                Ok(SessionEvent::RuntimeStarted {
                    runtime_id,
                    state: next,
                })
            }
            SessionCommand::RuntimeRetried {
                runtime_id,
                fallback_from_id,
            } => {
                let next = SessionState::Running;
                if previous != SessionState::Failed
                    || runtime_id.trim().is_empty()
                    || fallback_from_id.trim().is_empty()
                    || runtime_id == fallback_from_id
                    || self.active_runtime_id.is_some()
                    || self.runtime_ids.last() != Some(&fallback_from_id)
                {
                    return Err(SessionTransitionError { previous, next });
                }
                Ok(SessionEvent::RuntimeStarted {
                    runtime_id,
                    state: next,
                })
            }
            SessionCommand::RuntimeCompleted { runtime_id } => {
                let next = SessionState::Completed;
                if !self.runtime_terminal_matches(&runtime_id, next)
                    || !previous.can_transition_to(next)
                {
                    return Err(SessionTransitionError { previous, next });
                }
                Ok(SessionEvent::RuntimeCompleted {
                    runtime_id,
                    state: next,
                })
            }
            SessionCommand::RuntimeFailed { runtime_id } => {
                let next = SessionState::Failed;
                if !self.runtime_terminal_matches(&runtime_id, next)
                    || (previous != next && !previous.can_transition_to(next))
                {
                    return Err(SessionTransitionError { previous, next });
                }
                Ok(SessionEvent::RuntimeFailed {
                    runtime_id,
                    state: next,
                })
            }
            SessionCommand::RuntimeCancelled { runtime_id } => {
                let next = SessionState::Cancelled;
                if !self.runtime_terminal_matches(&runtime_id, next)
                    || (previous != next && !previous.can_transition_to(next))
                {
                    return Err(SessionTransitionError { previous, next });
                }
                Ok(SessionEvent::RuntimeCancelled {
                    runtime_id,
                    state: next,
                })
            }
            SessionCommand::RuntimeEnded { runtime_id } => {
                if runtime_id.trim().is_empty()
                    || self.active_runtime_id.as_ref() != Some(&runtime_id)
                    || !matches!(previous, SessionState::Running | SessionState::Paused)
                {
                    return Err(SessionTransitionError {
                        previous,
                        next: previous,
                    });
                }
                Ok(SessionEvent::RuntimeEnded {
                    runtime_id,
                    state: previous,
                })
            }
            SessionCommand::InterruptSession => {
                let mut task_plan = self.task_plan.clone();
                if !matches!(
                    previous,
                    SessionState::Completed
                        | SessionState::Failed
                        | SessionState::Cancelled
                        | SessionState::Interrupted
                ) {
                    for task in &mut task_plan.detailed_tasks {
                        if task.status == PlanStatus::Doing {
                            task.status = PlanStatus::WaitingUser;
                        }
                    }
                }
                Ok(SessionEvent::SessionInterrupted {
                    state: match previous {
                        SessionState::Completed
                        | SessionState::Failed
                        | SessionState::Cancelled
                        | SessionState::Interrupted => previous,
                        _ => SessionState::Interrupted,
                    },
                    task_plan,
                })
            }
            SessionCommand::CancelSession => Ok(SessionEvent::SessionCancelled {
                state: SessionState::Cancelled,
            }),
            SessionCommand::RegisterChildSession { parent_id } => {
                if !previous.can_transition_to(SessionState::Running) {
                    return Err(SessionTransitionError {
                        previous,
                        next: SessionState::Running,
                    });
                }
                Ok(SessionEvent::ChildSessionRegistered {
                    parent_id,
                    state: SessionState::Running,
                })
            }
            SessionCommand::ForkSession { parent_id } => {
                Ok(SessionEvent::SessionForked { parent_id })
            }
            SessionCommand::ApplyTaskStatus { task_plan } => {
                Ok(SessionEvent::TaskPlanChanged { task_plan })
            }
            SessionCommand::ApplyTaskPatch {
                patch,
                generated_task_id,
                now,
            } => {
                let mut task_plan = self.task_plan.clone();
                patch_one_task(&mut task_plan, patch, generated_task_id, now)?;
                Ok(SessionEvent::TaskPlanChanged { task_plan })
            }
            SessionCommand::ApplyTaskPatches {
                tasks,
                generated_task_ids,
                now,
            } => {
                let mut task_plan = self.task_plan.clone();
                patch_task_list(&mut task_plan, tasks, generated_task_ids, now)?;
                Ok(SessionEvent::TaskPlanChanged { task_plan })
            }
            SessionCommand::ApplyTaskPlanPatch { patch } => {
                let mut task_plan = self.task_plan.clone();
                if apply_task_plan_patch(&mut task_plan, self.state, patch).is_err() {
                    task_plan.clone_from(&self.task_plan);
                }
                Ok(SessionEvent::TaskPlanChanged { task_plan })
            }
            SessionCommand::StartScheduledTask {
                task_id,
                task_summary,
                start_condition,
                now,
            } => {
                let Some(index) = self.task_plan.detailed_tasks.iter().position(|task| {
                    task.task_id == task_id
                        && task.start_condition == start_condition
                        && task.display_summary(&self.task_plan.plan_summary) == task_summary
                        && task.scheduler_eligible(now)
                }) else {
                    return Err(SessionTransitionError {
                        previous,
                        next: previous,
                    });
                };
                if !previous.can_transition_to(SessionState::Running) {
                    return Err(SessionTransitionError {
                        previous,
                        next: SessionState::Running,
                    });
                }
                let mut task_plan = self.task_plan.clone();
                let plan_summary = task_plan.plan_summary.clone();
                let task = &mut task_plan.detailed_tasks[index];
                let task_id = task.task_id.clone();
                let task_summary = task.display_summary(&plan_summary);
                let start_condition = task.start_condition;
                task.status = PlanStatus::Doing;
                if matches!(start_condition, StartCondition::PollingTask) {
                    task.advance_polling_start(now);
                }
                Ok(SessionEvent::ScheduledTaskClaimed {
                    task_plan,
                    task_id,
                    task_summary,
                    start_condition,
                    state: SessionState::Running,
                })
            }
            SessionCommand::ClaimScheduledChildTask {
                expected_task_plan,
                claim,
                now,
            } => {
                if claim.validate().is_err()
                    || self.task_plan != expected_task_plan
                    || !self.task_plan.scheduling_identities_valid()
                {
                    return Err(SessionTransitionError {
                        previous,
                        next: previous,
                    });
                }
                let Some(index) = self.task_plan.detailed_tasks.iter().position(|task| {
                    task.task_id == claim.task_id && task.direct_dispatch_eligible(now)
                }) else {
                    return Err(SessionTransitionError {
                        previous,
                        next: previous,
                    });
                };
                let task = &self.task_plan.detailed_tasks[index];
                let Some(contract) = task.scheduling_contract.as_ref() else {
                    return Err(SessionTransitionError {
                        previous,
                        next: previous,
                    });
                };
                if task.dispatch_claim.is_some()
                    || !task.sub_session_id.is_empty()
                    || contract.validate(&task.task_id).is_err()
                    || contract.mission_id != claim.mission_id
                    || contract.semantic_dispatch_key != claim.semantic_dispatch_key
                    || contract.semantic_dispatch_sha256(&task.task_id)
                        != claim.semantic_dispatch_key
                    || contract.authority_mission_revision_sha256
                        != claim.authority_mission_revision_sha256
                    || contract.delegated_input_sha256 != claim.delegated_input_sha256
                    || contract.task_context_capsule_semantic_sha256
                        != claim.task_context_capsule_semantic_sha256
                    || task_plan_ready_set_sha256(&self.task_plan) != claim.parent_task_plan_sha256
                    || contract.contract_sha256() != claim.task_scheduling_contract_sha256
                    || contract.scope_claim_sha256() != claim.scope_claim_sha256
                {
                    return Err(SessionTransitionError {
                        previous,
                        next: previous,
                    });
                }
                if !previous.can_transition_to(SessionState::Running) {
                    return Err(SessionTransitionError {
                        previous,
                        next: SessionState::Running,
                    });
                }
                let mut task_plan = self.task_plan.clone();
                let task = &mut task_plan.detailed_tasks[index];
                task.status = PlanStatus::Doing;
                task.sub_session_id = claim.child_session_id.clone();
                task.dispatch_claim = Some(claim.clone());
                if matches!(task.start_condition, StartCondition::PollingTask) {
                    task.advance_polling_start(now);
                }
                Ok(SessionEvent::ScheduledChildTaskClaimed {
                    task_plan,
                    claim,
                    claimed_at: now,
                    state: SessionState::Running,
                })
            }
            SessionCommand::ConvergeAcknowledgedChildCallback { acknowledgment } => {
                let Ok(matching_index) =
                    self.acknowledged_child_callback_task_index(&acknowledgment)
                else {
                    return Err(SessionTransitionError {
                        previous,
                        next: previous,
                    });
                };
                let mut task_plan = self.task_plan.clone();
                let task = &mut task_plan.detailed_tasks[matching_index];
                match task.status {
                    PlanStatus::Doing => task.status = PlanStatus::Done,
                    PlanStatus::Done | PlanStatus::Archived => {}
                    PlanStatus::Todo | PlanStatus::WaitingUser | PlanStatus::Question => {
                        return Err(SessionTransitionError {
                            previous,
                            next: previous,
                        });
                    }
                }
                let all_tasks_terminal = task_plan
                    .detailed_tasks
                    .iter()
                    .all(|task| matches!(task.status, PlanStatus::Done | PlanStatus::Archived));
                let state = if previous == SessionState::Running
                    && self.active_runtime_id.is_none()
                    && all_tasks_terminal
                {
                    SessionState::Completed
                } else {
                    previous
                };
                Ok(SessionEvent::AcknowledgedChildCallbackConverged {
                    acknowledgment,
                    task_plan,
                    state,
                })
            }
            SessionCommand::DeleteSession => Ok(SessionEvent::SessionDeleted),
        }
    }

    pub fn apply(&mut self, event: &SessionEvent) {
        match event {
            SessionEvent::SessionCreated { task_plan } => {
                self.task_plan.clone_from(task_plan);
            }
            SessionEvent::SessionDeleted => {}
            SessionEvent::UserInputAccepted { state } => {
                self.state = *state;
                self.cancelled = false;
            }
            SessionEvent::UserTurnStarted { state } => {
                self.state = *state;
                self.cancelled = false;
            }
            SessionEvent::RuntimeStarted { runtime_id, state } => {
                self.state = *state;
                if !self.runtime_ids.contains(runtime_id) {
                    self.runtime_ids.push(runtime_id.clone());
                }
                self.active_runtime_id = Some(runtime_id.clone());
            }
            SessionEvent::RuntimeCompleted { runtime_id, state }
            | SessionEvent::RuntimeFailed { runtime_id, state } => {
                self.state = *state;
                if !self.runtime_ids.contains(runtime_id) {
                    self.runtime_ids.push(runtime_id.clone());
                }
                self.active_runtime_id = None;
            }
            SessionEvent::RuntimeCancelled { runtime_id, state } => {
                self.state = *state;
                if !self.runtime_ids.contains(runtime_id) {
                    self.runtime_ids.push(runtime_id.clone());
                }
                self.active_runtime_id = None;
                self.cancelled = true;
                self.pending_user_inputs.clear();
            }
            SessionEvent::RuntimeEnded { runtime_id, state } => {
                self.state = *state;
                if !self.runtime_ids.contains(runtime_id) {
                    self.runtime_ids.push(runtime_id.clone());
                }
                self.active_runtime_id = None;
            }
            SessionEvent::RuntimeStateApplied { state } => {
                self.state = *state;
                if state.is_terminal() {
                    self.active_runtime_id = None;
                }
            }
            SessionEvent::SessionInterrupted { state, task_plan } => {
                self.state = *state;
                self.task_plan.clone_from(task_plan);
                self.pending_user_inputs.clear();
                self.active_runtime_id = None;
            }
            SessionEvent::SessionCancelled { state } => {
                self.state = *state;
                self.cancelled = true;
                self.pending_user_inputs.clear();
                self.active_runtime_id = None;
            }
            SessionEvent::ChildSessionRegistered { parent_id, state } => {
                self.parent_id = Some(parent_id.clone());
                self.state = *state;
            }
            SessionEvent::SessionForked { parent_id } => {
                self.parent_id = Some(parent_id.clone());
            }
            SessionEvent::TaskPlanChanged { task_plan } => {
                self.task_plan.clone_from(task_plan);
            }
            SessionEvent::UserInputQueued { input } => {
                self.pending_user_inputs.push(input.clone());
                self.cancelled = false;
            }
            SessionEvent::QueuedUserInputsConsumed { .. } => self.pending_user_inputs.clear(),
            SessionEvent::ScheduledTaskClaimed {
                task_plan, state, ..
            }
            | SessionEvent::ScheduledChildTaskClaimed {
                task_plan, state, ..
            } => {
                self.task_plan.clone_from(task_plan);
                self.state = *state;
            }
            SessionEvent::AcknowledgedChildCallbackConverged {
                task_plan, state, ..
            } => {
                self.task_plan.clone_from(task_plan);
                self.state = *state;
            }
        }
    }

    pub fn query(&self, query: SessionQuery) -> SessionProjection {
        match query {
            SessionQuery::Lifecycle => SessionProjection {
                session_id: self.session_id.clone(),
                state: self.state,
                parent_id: self.parent_id.clone(),
                task_plan: self.task_plan.clone(),
                pending_user_inputs: self.pending_user_inputs.clone(),
                cancelled: self.cancelled,
                runtime_ids: self.runtime_ids.clone(),
                active_runtime_id: self.active_runtime_id.clone(),
            },
        }
    }

    fn runtime_terminal_matches(&self, runtime_id: &RuntimeId, next: SessionState) -> bool {
        if runtime_id.trim().is_empty() {
            return false;
        }
        match self.active_runtime_id.as_ref() {
            Some(active) => active == runtime_id,
            None if self.runtime_ids.contains(runtime_id) => self.state == next,
            None => false,
        }
    }

    fn scheduled_replay_command(&self, event: &SessionEvent) -> Option<SessionCommand> {
        let SessionEvent::ScheduledTaskClaimed {
            task_plan, task_id, ..
        } = event
        else {
            return None;
        };
        let task = self
            .task_plan
            .detailed_tasks
            .iter()
            .find(|task| task.task_id == *task_id)?;
        let now = match task.start_condition {
            StartCondition::ScheduledTask => task.start_at,
            StartCondition::PollingTask => {
                let result_task = task_plan
                    .detailed_tasks
                    .iter()
                    .find(|task| task.task_id == *task_id)?;
                let seconds = task
                    .poll_interval
                    .s
                    .saturating_add(task.poll_interval.m.saturating_mul(60))
                    .saturating_add(task.poll_interval.h.saturating_mul(60 * 60))
                    .saturating_add(task.poll_interval.d.saturating_mul(24 * 60 * 60))
                    .max(1);
                result_task
                    .start_at
                    .checked_sub_signed(Duration::seconds(seconds.min(i64::MAX as u64) as i64))?
            }
            StartCondition::SessionIdle => DateTime::<Utc>::MIN_UTC,
            StartCondition::UserAction => return None,
        };
        let command = SessionCommand::StartScheduledTask {
            task_id: task_id.clone(),
            task_summary: task.display_summary(&self.task_plan.plan_summary),
            start_condition: task.start_condition,
            now,
        };
        if self
            .decide(command.clone())
            .is_ok_and(|expected| expected == *event)
        {
            Some(command)
        } else {
            None
        }
    }
}

fn patch_one_task(
    task_plan: &mut TaskPlan,
    patch: SessionTaskPatch,
    generated_task_id: String,
    now: DateTime<Utc>,
) -> Result<(), SessionTransitionError> {
    if patch.scheduling_contract.is_some()
        && patch
            .task_id
            .as_deref()
            .is_none_or(|task_id| task_id.trim().is_empty())
    {
        return Err(task_patch_error());
    }
    let task_id = patch
        .task_id
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToString::to_string);
    if task_id.is_none() && task_plan.detailed_tasks.len() > 1 {
        return Err(task_patch_error());
    }

    let index = task_id.as_ref().and_then(|id| {
        task_plan
            .detailed_tasks
            .iter()
            .position(|task| &task.task_id == id)
    });
    let index = match index {
        Some(index) => index,
        None if task_id.is_some() => {
            task_plan.detailed_tasks.push(TaskStep {
                task_id: task_id.unwrap_or(generated_task_id),
                step: patch
                    .step
                    .unwrap_or(task_plan.detailed_tasks.len() as u64 + 1),
                start_at: now,
                ..TaskStep::default()
            });
            task_plan.detailed_tasks.len() - 1
        }
        None if task_plan.detailed_tasks.is_empty() => {
            let summary = task_plan.plan_summary.clone();
            task_plan.detailed_tasks.push(TaskStep {
                task_id: generated_task_id,
                step: 1,
                start_at: now,
                task_summary: summary.clone(),
                step_task: summary,
                ..TaskStep::default()
            });
            0
        }
        None => 0,
    };
    apply_task_patch(&mut task_plan.detailed_tasks[index], patch)?;
    if task_plan.plan_summary.trim().is_empty() {
        task_plan.plan_summary = task_plan.detailed_tasks[index].task_summary.clone();
    }
    Ok(())
}

fn apply_task_plan_patch(
    task_plan: &mut TaskPlan,
    state: SessionState,
    patch: SessionTaskPlanPatch,
) -> Result<(), SessionTransitionError> {
    let existing_ids = task_plan
        .detailed_tasks
        .iter()
        .map(|task| task.task_id.clone())
        .collect::<Vec<_>>();
    let doing_ids = task_plan
        .detailed_tasks
        .iter()
        .filter(|task| task.status == PlanStatus::Doing)
        .map(|task| task.task_id.clone())
        .collect::<Vec<_>>();
    if let Some(tasks) = patch.tasks {
        patch_task_list(task_plan, tasks, patch.generated_task_ids, patch.now)?;
    }
    if let Some(summary) = patch
        .plan_summary
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
    {
        task_plan.plan_summary = summary;
    }
    if let Some(task) = patch.task {
        patch_one_task(task_plan, task, patch.generated_task_id, patch.now)?;
    }
    if matches!(state, SessionState::Running | SessionState::Paused) {
        for task in &mut task_plan.detailed_tasks {
            if doing_ids.contains(&task.task_id)
                && matches!(task.status, PlanStatus::Todo | PlanStatus::Question)
            {
                task.status = PlanStatus::Doing;
            }
        }
    } else {
        for task in &mut task_plan.detailed_tasks {
            if !existing_ids.contains(&task.task_id)
                && task.start_condition == StartCondition::SessionIdle
                && matches!(task.status, PlanStatus::Todo | PlanStatus::Question)
            {
                task.status = PlanStatus::WaitingUser;
            }
        }
    }
    Ok(())
}

fn patch_task_list(
    task_plan: &mut TaskPlan,
    patches: Vec<SessionTaskPatch>,
    generated_task_ids: Vec<String>,
    now: DateTime<Utc>,
) -> Result<(), SessionTransitionError> {
    if generated_task_ids.len() != patches.len() {
        return Err(task_patch_error());
    }
    let mut requested_order = Vec::new();
    for (patch, generated_task_id) in patches.into_iter().zip(generated_task_ids) {
        if patch.scheduling_contract.is_some()
            && patch
                .task_id
                .as_deref()
                .is_none_or(|task_id| task_id.trim().is_empty())
        {
            return Err(task_patch_error());
        }
        let task_id = patch
            .task_id
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(ToString::to_string)
            .unwrap_or(generated_task_id);
        let existing = task_plan
            .detailed_tasks
            .iter()
            .position(|task| task.task_id == task_id);
        if existing.is_some() && !requested_order.contains(&task_id) {
            requested_order.push(task_id.clone());
        }
        let index = existing.unwrap_or_else(|| {
            task_plan.detailed_tasks.push(TaskStep {
                task_id: task_id.clone(),
                step: patch
                    .step
                    .unwrap_or(task_plan.detailed_tasks.len() as u64 + 1),
                start_at: now,
                ..TaskStep::default()
            });
            task_plan.detailed_tasks.len() - 1
        });
        apply_task_patch(&mut task_plan.detailed_tasks[index], patch)?;
    }
    task_plan.detailed_tasks.sort_by_key(|task| {
        requested_order
            .iter()
            .position(|task_id| task_id == &task.task_id)
            .unwrap_or(usize::MAX)
    });
    for (index, task) in task_plan.detailed_tasks.iter_mut().enumerate() {
        task.step = index as u64 + 1;
    }
    Ok(())
}

fn apply_task_patch(
    task: &mut TaskStep,
    patch: SessionTaskPatch,
) -> Result<(), SessionTransitionError> {
    if let Some(claim) = task.dispatch_claim.as_ref() {
        let task_identity_changes = patch
            .task_id
            .as_deref()
            .filter(|value| !value.trim().is_empty())
            .is_some_and(|task_id| task_id != task.task_id);
        let child_identity_changes = patch
            .sub_session_id
            .as_deref()
            .filter(|value| !value.trim().is_empty())
            .is_some_and(|child_session_id| child_session_id != claim.child_session_id);
        let scheduling_identity_changes = patch
            .scheduling_contract
            .as_ref()
            .is_some_and(|contract| Some(contract) != task.scheduling_contract.as_ref());
        if task_identity_changes || child_identity_changes || scheduling_identity_changes {
            return Err(task_patch_error());
        }
    }
    if let Some(task_id) = patch.task_id.filter(|value| !value.trim().is_empty()) {
        task.task_id = task_id;
    }
    if let Some(step) = patch.step {
        task.step = step;
    }
    if let Some(summary) = patch.task_summary.filter(|value| !value.trim().is_empty()) {
        task.task_summary.clone_from(&summary);
        if task.step_task.trim().is_empty() {
            task.step_task = summary;
        }
    }
    if let Some(deliverable) = patch.deliverable.filter(|value| !value.trim().is_empty()) {
        task.step_deliverable_description = deliverable;
    }
    if let Some(sub_session_id) = patch
        .sub_session_id
        .filter(|value| !value.trim().is_empty())
    {
        task.sub_session_id = sub_session_id;
    }
    if let Some(status) = patch.status {
        task.status = status;
    }
    if let Some(scheduling_contract) = patch.scheduling_contract {
        task.scheduling_contract = Some(scheduling_contract);
    }
    if let Some(poll_interval) = patch.poll_interval {
        task.poll_interval = poll_interval;
        if poll_interval != PollInterval::default() {
            task.start_condition = StartCondition::PollingTask;
        } else if matches!(task.start_condition, StartCondition::PollingTask) {
            task.start_condition = StartCondition::UserAction;
        }
    }
    if let Some(start_at) = patch.start_at {
        task.start_at = start_at;
        if !matches!(task.start_condition, StartCondition::PollingTask) {
            task.start_condition = StartCondition::ScheduledTask;
        }
    }
    if let Some(start_condition) = patch.start_condition {
        task.start_condition = start_condition;
    }
    Ok(())
}

fn task_patch_error() -> SessionTransitionError {
    SessionTransitionError {
        previous: SessionState::Created,
        next: SessionState::Created,
    }
}

impl SessionState {
    pub fn can_transition_to(self, next: Self) -> bool {
        use SessionState::*;

        match (self, next) {
            (Created, Running | Cancelled) => true,
            (Running, Paused | Completed | Failed | Cancelled | Interrupted) => true,
            (Paused, Running | Cancelled | Failed | Interrupted) => true,
            (Completed, Created | Running) => true,
            (Failed | Cancelled | Interrupted, _) => false,
            _ if self == next => true,
            _ => false,
        }
    }

    pub fn ui_status(self) -> &'static str {
        match self {
            Self::Created | Self::Completed => "idle",
            Self::Running | Self::Paused => "busy",
            Self::Failed | Self::Cancelled | Self::Interrupted => "error",
        }
    }

    pub fn is_recoverable_running(self) -> bool {
        matches!(self, Self::Running | Self::Paused)
    }

    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Completed | Self::Failed | Self::Cancelled | Self::Interrupted
        )
    }
}

#[cfg(test)]
mod tests {
    use chrono::Utc;
    use std::collections::BTreeSet;

    use super::{
        ACKNOWLEDGED_CHILD_CALLBACK_IDENTITY_SCHEMA_VERSION, AcknowledgedChildCallbackIdentityV1,
        PlanStatus, PollInterval, SessionAggregate, SessionCommand, SessionEvent,
        SessionProjection, SessionQuery, SessionState, SessionTaskPatch, StartCondition,
        TASK_DISPATCH_CLAIM_SCHEMA_VERSION, TASK_SCHEDULING_CONTRACT_SCHEMA_VERSION,
        TaskDispatchClaimV1, TaskLeaseReadinessEvidence, TaskPlan, TaskReadySetEvidence,
        TaskReadySetState, TaskSchedulingContractV1, TaskStep, classify_task_ready_set,
        task_plan_ready_set_sha256,
    };

    fn aggregate_in_state(state: SessionState) -> SessionAggregate {
        let mut aggregate = SessionAggregate::new("session-fixed".to_string());
        aggregate.state = state;
        aggregate
    }

    fn scheduling_contract(
        task_id: &str,
        authority: char,
        dependencies: Vec<String>,
        conflict_identities: Vec<String>,
    ) -> TaskSchedulingContractV1 {
        let mut contract = TaskSchedulingContractV1 {
            schema_version: TASK_SCHEDULING_CONTRACT_SCHEMA_VERSION.to_string(),
            mission_id: "mission-a".to_string(),
            semantic_dispatch_key: "b".repeat(64),
            authority_mission_revision_sha256: authority.to_string().repeat(64),
            delegated_input_sha256: "d".repeat(64),
            task_context_capsule_semantic_sha256: "e".repeat(64),
            dependency_task_ids: dependencies,
            exact_input_sha256s: vec![],
            jspace_authorization_semantic_sha256: "c".repeat(64),
            read_scopes: vec!["src/**".to_string()],
            write_scopes: vec![],
            declared_targets: vec![],
            conflict_identities,
            maximum_parallel_runtime_workers: 24,
        };
        contract.semantic_dispatch_key = contract.semantic_dispatch_sha256(task_id);
        contract
    }

    fn canonical_scope_entries(count: usize) -> Vec<String> {
        (0..count)
            .map(|index| format!("scope/{index:04}"))
            .collect()
    }

    fn ready_evidence() -> TaskReadySetEvidence {
        TaskReadySetEvidence {
            evaluated_at_ms: Utc::now().timestamp_millis(),
            parent_session_can_transition_to_running: true,
            authority_mission_revision_sha256: "a".repeat(64),
            execution_terminal_task_ids: BTreeSet::new(),
            available_exact_input_sha256s: Some(BTreeSet::new()),
            semantic_dispatch_identity_valid: true,
            candidate_scope_identity_valid: true,
            parallel_runtime_limit_supported: true,
            task_leases: vec![],
            lease_unknown_task_ids: BTreeSet::new(),
            scope_evaluated_task_ids: BTreeSet::new(),
            scope_conflicting_task_ids: BTreeSet::new(),
            scope_unknown_task_ids: BTreeSet::new(),
            active_runtime_workers: 0,
            requested_lease_id: Some("lease-new".to_string()),
            claimed_task_id: None,
        }
    }

    fn dispatch_claim(task_plan: &TaskPlan) -> TaskDispatchClaimV1 {
        let task = task_plan
            .detailed_tasks
            .iter()
            .find(|task| task.task_id == "task-b")
            .expect("task-b fixture");
        let contract = task.scheduling_contract.as_ref().expect("contract fixture");
        TaskDispatchClaimV1 {
            schema_version: TASK_DISPATCH_CLAIM_SCHEMA_VERSION.to_string(),
            mission_id: "mission-a".to_string(),
            task_id: "task-b".to_string(),
            child_session_id: "child-b".to_string(),
            child_runtime_id: "runtime-b".to_string(),
            child_lease_id: "lease-b".to_string(),
            child_transaction_id: "transaction-b".to_string(),
            semantic_dispatch_key: contract.semantic_dispatch_key.clone(),
            authority_mission_revision_sha256: "a".repeat(64),
            delegated_input_sha256: "d".repeat(64),
            task_context_capsule_semantic_sha256: "e".repeat(64),
            parent_task_plan_sha256: task_plan_ready_set_sha256(task_plan),
            task_scheduling_contract_sha256: contract.contract_sha256(),
            scope_claim_sha256: contract.scope_claim_sha256(),
        }
    }

    fn acknowledged_child_callback_identity() -> AcknowledgedChildCallbackIdentityV1 {
        AcknowledgedChildCallbackIdentityV1 {
            schema_version: ACKNOWLEDGED_CHILD_CALLBACK_IDENTITY_SCHEMA_VERSION.to_string(),
            parent_mission_revision_sha256: "a".repeat(64),
            commander_thread_id: "commander-thread".to_string(),
            child_session_id: "callback-child".to_string(),
            child_runtime_id: "callback-runtime".to_string(),
            child_lease_id: "callback-lease".to_string(),
            child_transaction_id: "callback-transaction".to_string(),
            event_id: "callback-event".to_string(),
            callback_payload_sha256: "b".repeat(64),
            effect_identity_sha256: "c".repeat(64),
        }
    }

    fn child_callback_claim(
        task_id: &str,
        acknowledgment: &AcknowledgedChildCallbackIdentityV1,
    ) -> TaskDispatchClaimV1 {
        TaskDispatchClaimV1 {
            schema_version: TASK_DISPATCH_CLAIM_SCHEMA_VERSION.to_string(),
            mission_id: "callback-mission".to_string(),
            task_id: task_id.to_string(),
            child_session_id: acknowledgment.child_session_id.clone(),
            child_runtime_id: acknowledgment.child_runtime_id.clone(),
            child_lease_id: acknowledgment.child_lease_id.clone(),
            child_transaction_id: acknowledgment.child_transaction_id.clone(),
            semantic_dispatch_key: "d".repeat(64),
            authority_mission_revision_sha256: acknowledgment
                .parent_mission_revision_sha256
                .clone(),
            delegated_input_sha256: "e".repeat(64),
            task_context_capsule_semantic_sha256: "f".repeat(64),
            parent_task_plan_sha256: "1".repeat(64),
            task_scheduling_contract_sha256: "2".repeat(64),
            scope_claim_sha256: "3".repeat(64),
        }
    }

    fn child_callback_task(
        task_id: &str,
        status: PlanStatus,
        acknowledgment: &AcknowledgedChildCallbackIdentityV1,
    ) -> TaskStep {
        TaskStep {
            task_id: task_id.to_string(),
            sub_session_id: acknowledgment.child_session_id.clone(),
            status,
            dispatch_claim: Some(child_callback_claim(task_id, acknowledgment)),
            task_summary: format!("Await {task_id}"),
            ..TaskStep::default()
        }
    }

    fn child_callback_parent(tasks: Vec<TaskStep>) -> SessionAggregate {
        let mut aggregate = SessionAggregate::new("callback-parent".to_string());
        aggregate.apply(&SessionEvent::SessionCreated {
            task_plan: TaskPlan {
                plan_summary: "Callback parent".to_string(),
                detailed_tasks: tasks,
            },
        });
        aggregate
            .execute(SessionCommand::ApplyRuntimeState {
                state: SessionState::Running,
            })
            .expect("callback parent should be running");
        aggregate
    }

    #[test]
    fn child_callback_ack_atomically_completes_exact_task_and_parent() {
        let acknowledgment = acknowledged_child_callback_identity();
        let task_plan = TaskPlan {
            plan_summary: "Callback parent".to_string(),
            detailed_tasks: vec![child_callback_task(
                "callback-task",
                PlanStatus::Doing,
                &acknowledgment,
            )],
        };
        let created = SessionEvent::SessionCreated {
            task_plan: task_plan.clone(),
        };
        let mut aggregate = SessionAggregate::new("callback-parent".to_string());
        aggregate.apply(&created);
        let running = aggregate
            .execute(SessionCommand::ApplyRuntimeState {
                state: SessionState::Running,
            })
            .expect("callback parent should run");
        let event = aggregate
            .execute(SessionCommand::ConvergeAcknowledgedChildCallback {
                acknowledgment: acknowledgment.clone(),
            })
            .expect("exact callback acknowledgment should converge");

        assert!(matches!(
            &event,
            SessionEvent::AcknowledgedChildCallbackConverged {
                acknowledgment: stored,
                state: SessionState::Completed,
                ..
            } if stored == &acknowledgment
        ));
        assert_eq!(
            aggregate.task_plan.detailed_tasks[0].status,
            PlanStatus::Done
        );
        assert_eq!(aggregate.state, SessionState::Completed);
        assert!(aggregate.active_runtime_id.is_none());
        let replayed =
            SessionAggregate::replay("callback-parent".to_string(), [created, running, event])
                .expect("compound callback event should replay canonically");
        assert_eq!(replayed, aggregate);
    }

    #[test]
    fn child_callback_ack_preserves_nonterminal_sibling_and_active_runtime() {
        let acknowledgment = acknowledged_child_callback_identity();
        let mut with_sibling = child_callback_parent(vec![
            child_callback_task("callback-task", PlanStatus::Doing, &acknowledgment),
            TaskStep {
                task_id: "sibling-task".to_string(),
                status: PlanStatus::Todo,
                task_summary: "Continue parent work".to_string(),
                ..TaskStep::default()
            },
        ]);
        with_sibling
            .execute(SessionCommand::ConvergeAcknowledgedChildCallback {
                acknowledgment: acknowledgment.clone(),
            })
            .expect("exact callback should complete only its task");
        assert_eq!(
            with_sibling.task_plan.detailed_tasks[0].status,
            PlanStatus::Done
        );
        assert_eq!(
            with_sibling.task_plan.detailed_tasks[1].status,
            PlanStatus::Todo
        );
        assert_eq!(with_sibling.state, SessionState::Running);

        let mut with_runtime = child_callback_parent(vec![child_callback_task(
            "callback-task",
            PlanStatus::Doing,
            &acknowledgment,
        )]);
        with_runtime
            .execute(SessionCommand::RuntimeStarted {
                runtime_id: "parent-runtime".to_string(),
            })
            .expect("parent runtime should become active");
        with_runtime
            .execute(SessionCommand::ConvergeAcknowledgedChildCallback { acknowledgment })
            .expect("callback task should complete while runtime remains active");
        assert_eq!(
            with_runtime.task_plan.detailed_tasks[0].status,
            PlanStatus::Done
        );
        assert_eq!(with_runtime.state, SessionState::Running);
        assert_eq!(
            with_runtime.active_runtime_id.as_deref(),
            Some("parent-runtime")
        );
    }

    #[test]
    fn child_callback_ack_missing_ambiguous_and_mismatched_claims_fail_closed() {
        let acknowledgment = acknowledged_child_callback_identity();
        let mut missing = child_callback_parent(vec![TaskStep {
            task_id: "unclaimed-task".to_string(),
            sub_session_id: acknowledgment.child_session_id.clone(),
            status: PlanStatus::Doing,
            ..TaskStep::default()
        }]);
        let before_missing = missing.clone();
        assert!(
            missing
                .execute(SessionCommand::ConvergeAcknowledgedChildCallback {
                    acknowledgment: acknowledgment.clone(),
                })
                .is_err()
        );
        assert_eq!(missing, before_missing);

        let mut mismatched_task =
            child_callback_task("mismatched-task", PlanStatus::Doing, &acknowledgment);
        mismatched_task
            .dispatch_claim
            .as_mut()
            .expect("claim fixture")
            .child_runtime_id = "different-runtime".to_string();
        let mut mismatched = child_callback_parent(vec![mismatched_task]);
        let before_mismatch = mismatched.clone();
        assert!(
            mismatched
                .execute(SessionCommand::ConvergeAcknowledgedChildCallback {
                    acknowledgment: acknowledgment.clone(),
                })
                .is_err()
        );
        assert_eq!(mismatched, before_mismatch);

        let mut ambiguous = child_callback_parent(vec![
            child_callback_task("callback-one", PlanStatus::Doing, &acknowledgment),
            child_callback_task("callback-two", PlanStatus::Doing, &acknowledgment),
        ]);
        let before_ambiguous = ambiguous.clone();
        assert!(
            ambiguous
                .execute(SessionCommand::ConvergeAcknowledgedChildCallback { acknowledgment })
                .is_err()
        );
        assert_eq!(ambiguous, before_ambiguous);
    }

    #[test]
    fn child_callback_ack_repairs_already_done_task_without_unarchiving() {
        let acknowledgment = acknowledged_child_callback_identity();
        let mut done = child_callback_parent(vec![child_callback_task(
            "done-task",
            PlanStatus::Done,
            &acknowledgment,
        )]);
        done.execute(SessionCommand::ConvergeAcknowledgedChildCallback {
            acknowledgment: acknowledgment.clone(),
        })
        .expect("already-done task should still close its running parent");
        assert_eq!(done.state, SessionState::Completed);

        let mut archived = child_callback_parent(vec![child_callback_task(
            "archived-task",
            PlanStatus::Archived,
            &acknowledgment,
        )]);
        archived
            .execute(SessionCommand::ConvergeAcknowledgedChildCallback { acknowledgment })
            .expect("archived task should remain terminal while closing its parent");
        assert_eq!(
            archived.task_plan.detailed_tasks[0].status,
            PlanStatus::Archived
        );
        assert_eq!(archived.state, SessionState::Completed);
    }

    #[test]
    fn child_callback_ack_rejects_late_ack_for_newer_task_states() {
        let acknowledgment = acknowledged_child_callback_identity();

        for status in [
            PlanStatus::Todo,
            PlanStatus::WaitingUser,
            PlanStatus::Question,
        ] {
            let mut aggregate = child_callback_parent(vec![child_callback_task(
                "late-ack-task",
                status,
                &acknowledgment,
            )]);
            let before = aggregate.clone();

            assert_eq!(
                aggregate.validate_acknowledged_child_callback(&acknowledgment),
                Err("ACKNOWLEDGED_CHILD_CALLBACK_TASK_STATUS_CONFLICT")
            );
            assert!(
                aggregate
                    .execute(SessionCommand::ConvergeAcknowledgedChildCallback {
                        acknowledgment: acknowledgment.clone(),
                    })
                    .is_err(),
                "late ACK must fail closed for {status:?}"
            );
            assert_eq!(aggregate, before);
        }
    }

    #[test]
    fn scheduling_contract_is_bounded_canonical_and_fail_closed() {
        let contract = scheduling_contract("task-b", 'a', vec!["task-a".to_string()], vec![]);
        contract.validate("task-b").expect("valid contract");

        let identity = contract.semantic_dispatch_identity("task-b");
        let mut changed_dependency = contract.clone();
        changed_dependency.dependency_task_ids.clear();
        assert_ne!(
            identity,
            changed_dependency.semantic_dispatch_identity("task-b"),
            "dependency graph must be part of semantic dispatch identity"
        );

        let mut duplicate = contract.clone();
        duplicate.dependency_task_ids = vec!["task-a".to_string(), "task-a".to_string()];
        assert_eq!(
            duplicate.validate("task-b"),
            Err("TASK_SCHEDULING_DEPENDENCIES_INVALID")
        );
        let mut self_cycle = contract;
        self_cycle.dependency_task_ids = vec!["task-b".to_string()];
        assert_eq!(
            self_cycle.validate("task-b"),
            Err("TASK_SCHEDULING_DEPENDENCIES_INVALID")
        );
    }

    #[test]
    fn scheduling_contract_accepts_1024_canonical_entries_for_each_scope_kind() {
        let scopes = canonical_scope_entries(1_024);
        let mut contract = scheduling_contract("task-b", 'a', vec![], vec![]);
        contract.read_scopes = scopes.clone();
        contract.write_scopes = scopes.clone();
        contract.declared_targets = scopes;

        contract
            .validate("task-b")
            .expect("exactly 1024 canonical entries per scope kind must be accepted");
    }

    #[test]
    fn scheduling_contract_rejects_1025_entries_for_each_scope_kind() {
        let scopes = canonical_scope_entries(1_025);

        let mut read_contract = scheduling_contract("task-b", 'a', vec![], vec![]);
        read_contract.read_scopes = scopes.clone();
        assert_eq!(
            read_contract.validate("task-b"),
            Err("TASK_SCHEDULING_SCOPE_INVALID")
        );

        let mut write_contract = scheduling_contract("task-b", 'a', vec![], vec![]);
        write_contract.write_scopes = scopes.clone();
        assert_eq!(
            write_contract.validate("task-b"),
            Err("TASK_SCHEDULING_SCOPE_INVALID")
        );

        let mut target_contract = scheduling_contract("task-b", 'a', vec![], vec![]);
        target_contract.declared_targets = scopes;
        assert_eq!(
            target_contract.validate("task-b"),
            Err("TASK_SCHEDULING_SCOPE_INVALID")
        );
    }

    #[test]
    fn ready_set_classifies_dependency_scope_lease_and_unknown_without_guessing() {
        let dependency = TaskStep {
            task_id: "task-a".to_string(),
            status: PlanStatus::Doing,
            sub_session_id: "child-a".to_string(),
            scheduling_contract: Some(scheduling_contract("task-a", 'a', vec![], vec![])),
            ..TaskStep::default()
        };
        let candidate = TaskStep {
            task_id: "task-b".to_string(),
            start_condition: StartCondition::SessionIdle,
            scheduling_contract: Some(scheduling_contract(
                "task-b",
                'a',
                vec!["task-a".to_string()],
                vec![],
            )),
            ..TaskStep::default()
        };
        let plan = TaskPlan {
            plan_summary: "plan".to_string(),
            detailed_tasks: vec![dependency, candidate],
        };

        let mut terminal_parent = ready_evidence();
        terminal_parent.parent_session_can_transition_to_running = false;
        let terminal = classify_task_ready_set(&plan, "task-b", &terminal_parent);
        assert_eq!(terminal.state, TaskReadySetState::BlockedState);
        assert_eq!(
            terminal.reason_codes,
            vec!["TASK_READY_SET_PARENT_SESSION_TERMINAL"]
        );

        let mut invalid_scope = ready_evidence();
        invalid_scope.candidate_scope_identity_valid = false;
        let invalid_scope = classify_task_ready_set(&plan, "task-b", &invalid_scope);
        assert_eq!(invalid_scope.state, TaskReadySetState::TypedUnknown);
        assert_eq!(
            invalid_scope.reason_codes,
            vec!["TASK_READY_SET_SCOPE_IDENTITY_INVALID"]
        );

        let active_dependency = classify_task_ready_set(&plan, "task-b", &ready_evidence());
        assert_eq!(
            active_dependency.state,
            TaskReadySetState::BlockedDependency
        );

        let mut ready = ready_evidence();
        ready
            .execution_terminal_task_ids
            .insert("task-a".to_string());
        assert_eq!(
            classify_task_ready_set(&plan, "task-b", &ready).state,
            TaskReadySetState::Ready
        );

        ready.task_leases.push(TaskLeaseReadinessEvidence {
            task_id: "task-b".to_string(),
            lease_id: "lease-existing".to_string(),
            lease_active: true,
            terminal: false,
        });
        assert_eq!(
            classify_task_ready_set(&plan, "task-b", &ready).state,
            TaskReadySetState::BlockedLease
        );

        ready.task_leases.clear();
        ready.task_leases.push(TaskLeaseReadinessEvidence {
            task_id: "task-a".to_string(),
            lease_id: "lease-a".to_string(),
            lease_active: true,
            terminal: false,
        });
        assert_eq!(
            classify_task_ready_set(&plan, "task-b", &ready).state,
            TaskReadySetState::TypedUnknown
        );
        ready.scope_evaluated_task_ids.insert("task-a".to_string());
        ready
            .scope_conflicting_task_ids
            .insert("task-a".to_string());
        assert_eq!(
            classify_task_ready_set(&plan, "task-b", &ready).state,
            TaskReadySetState::BlockedScope
        );

        let duplicate_identity_plan = TaskPlan {
            plan_summary: "duplicate identities".to_string(),
            detailed_tasks: vec![
                plan.detailed_tasks[0].clone(),
                plan.detailed_tasks[0].clone(),
            ],
        };
        let duplicate =
            classify_task_ready_set(&duplicate_identity_plan, "task-a", &ready_evidence());
        assert_eq!(duplicate.state, TaskReadySetState::TypedUnknown);
        assert_eq!(
            duplicate.reason_codes,
            vec!["TASK_READY_SET_TASK_IDENTITIES_INVALID"]
        );
    }

    #[test]
    fn terminal_dependency_does_not_require_callback_acknowledgment() {
        let dependency = TaskStep {
            task_id: "task-a".to_string(),
            status: PlanStatus::Done,
            sub_session_id: "child-a".to_string(),
            scheduling_contract: Some(scheduling_contract("task-a", 'a', vec![], vec![])),
            ..TaskStep::default()
        };
        let candidate = TaskStep {
            task_id: "task-b".to_string(),
            start_condition: StartCondition::SessionIdle,
            scheduling_contract: Some(scheduling_contract(
                "task-b",
                'a',
                vec!["task-a".to_string()],
                vec![],
            )),
            ..TaskStep::default()
        };
        let plan = TaskPlan {
            plan_summary: "plan".to_string(),
            detailed_tasks: vec![dependency, candidate],
        };

        assert_eq!(
            classify_task_ready_set(&plan, "task-b", &ready_evidence()).state,
            TaskReadySetState::Ready
        );
    }

    #[test]
    fn task_ready_set_user_action_dispatch_is_single_owner_and_replayable() {
        let now = Utc::now();
        for start_condition in [
            StartCondition::SessionIdle,
            StartCondition::ScheduledTask,
            StartCondition::PollingTask,
        ] {
            let ordinary_task = TaskStep {
                start_at: now,
                start_condition,
                ..TaskStep::default()
            };
            assert!(ordinary_task.scheduler_eligible(now));
            assert!(ordinary_task.direct_dispatch_eligible(now));
        }
        let task_plan = TaskPlan {
            plan_summary: "plan".to_string(),
            detailed_tasks: vec![TaskStep {
                task_id: "task-b".to_string(),
                start_condition: StartCondition::UserAction,
                scheduling_contract: Some(scheduling_contract("task-b", 'a', vec![], vec![])),
                ..TaskStep::default()
            }],
        };
        assert!(!task_plan.detailed_tasks[0].scheduler_eligible(now));
        let mut scheduler_read = ready_evidence();
        scheduler_read.requested_lease_id = None;
        let scheduler_decision = classify_task_ready_set(&task_plan, "task-b", &scheduler_read);
        assert_eq!(scheduler_decision.state, TaskReadySetState::BlockedState);
        assert_eq!(
            scheduler_decision.reason_codes,
            vec!["TASK_READY_SET_USER_ACTION_REQUIRED"]
        );
        assert_eq!(
            classify_task_ready_set(&task_plan, "task-b", &ready_evidence()).state,
            TaskReadySetState::Ready
        );
        let created = SessionEvent::SessionCreated {
            task_plan: task_plan.clone(),
        };
        let mut aggregate = SessionAggregate::new("session-fixed".to_string());
        aggregate.apply(&created);
        let claim = dispatch_claim(&task_plan);
        let event = aggregate
            .execute(SessionCommand::ClaimScheduledChildTask {
                expected_task_plan: task_plan.clone(),
                claim: claim.clone(),
                now: Utc::now(),
            })
            .expect("exact claim");
        assert!(matches!(
            &event,
            SessionEvent::ScheduledChildTaskClaimed { claim: stored, .. }
                if stored == &claim
        ));
        let task = &aggregate.task_plan.detailed_tasks[0];
        assert_eq!(task.status, PlanStatus::Doing);
        assert_eq!(task.sub_session_id, "child-b");
        assert_eq!(task.dispatch_claim.as_ref(), Some(&claim));

        let replayed = SessionAggregate::replay("session-fixed".to_string(), [created, event])
            .expect("canonical claim event replay");
        assert_eq!(replayed.task_plan, aggregate.task_plan);

        let post_claim = aggregate.clone();
        let mut conflicting = claim;
        conflicting.child_session_id = "child-other".to_string();
        assert!(
            aggregate
                .execute(SessionCommand::ClaimScheduledChildTask {
                    expected_task_plan: task_plan,
                    claim: conflicting,
                    now: Utc::now(),
                })
                .is_err()
        );
        assert_eq!(aggregate, post_claim);
    }

    #[test]
    fn late_polling_child_claim_advances_schedule_past_admission_time() {
        let admitted_at = Utc::now();
        let task_plan = TaskPlan {
            plan_summary: "late polling plan".to_string(),
            detailed_tasks: vec![TaskStep {
                task_id: "task-b".to_string(),
                start_at: admitted_at - chrono::Duration::minutes(35),
                start_condition: StartCondition::PollingTask,
                poll_interval: PollInterval {
                    m: 5,
                    ..PollInterval::default()
                },
                scheduling_contract: Some(scheduling_contract("task-b", 'a', vec![], vec![])),
                ..TaskStep::default()
            }],
        };
        let mut aggregate = SessionAggregate::new("session-fixed".to_string());
        aggregate.apply(&SessionEvent::SessionCreated {
            task_plan: task_plan.clone(),
        });
        let claim = dispatch_claim(&task_plan);

        aggregate
            .execute(SessionCommand::ClaimScheduledChildTask {
                expected_task_plan: task_plan,
                claim,
                now: admitted_at,
            })
            .expect("late polling child claim");

        let task = &aggregate.task_plan.detailed_tasks[0];
        assert_eq!(task.status, PlanStatus::Doing);
        assert!(task.start_at > admitted_at);
        assert_eq!(task.start_at, admitted_at + chrono::Duration::minutes(5));
    }

    #[test]
    fn claimed_task_patch_rejects_identity_mutation_without_state_change() {
        let task_plan = TaskPlan {
            plan_summary: "claimed plan".to_string(),
            detailed_tasks: vec![TaskStep {
                task_id: "task-b".to_string(),
                start_condition: StartCondition::SessionIdle,
                scheduling_contract: Some(scheduling_contract("task-b", 'a', vec![], vec![])),
                ..TaskStep::default()
            }],
        };
        let mut claimed = SessionAggregate::new("session-fixed".to_string());
        claimed.apply(&SessionEvent::SessionCreated {
            task_plan: task_plan.clone(),
        });
        let claim = dispatch_claim(&claimed.task_plan);
        claimed
            .execute(SessionCommand::ClaimScheduledChildTask {
                expected_task_plan: task_plan,
                claim,
                now: Utc::now(),
            })
            .expect("claim task identity");
        let claimed_preimage = claimed.clone();

        let mut changed_child = claimed.clone();
        assert!(
            changed_child
                .execute(SessionCommand::ApplyTaskPatch {
                    patch: SessionTaskPatch {
                        task_id: Some("task-b".to_string()),
                        sub_session_id: Some("child-other".to_string()),
                        ..SessionTaskPatch::default()
                    },
                    generated_task_id: "unused".to_string(),
                    now: Utc::now(),
                })
                .is_err()
        );
        assert_eq!(changed_child, claimed_preimage);

        let mut changed_contract = claimed.clone();
        assert!(
            changed_contract
                .execute(SessionCommand::ApplyTaskPatch {
                    patch: SessionTaskPatch {
                        task_id: Some("task-b".to_string()),
                        scheduling_contract: Some(scheduling_contract(
                            "task-b",
                            'b',
                            vec![],
                            vec![],
                        )),
                        ..SessionTaskPatch::default()
                    },
                    generated_task_id: "unused".to_string(),
                    now: Utc::now(),
                })
                .is_err()
        );
        assert_eq!(changed_contract, claimed_preimage);

        let current_contract = claimed.task_plan.detailed_tasks[0]
            .scheduling_contract
            .clone()
            .expect("claimed contract");
        claimed
            .execute(SessionCommand::ApplyTaskPatch {
                patch: SessionTaskPatch {
                    task_id: Some("task-b".to_string()),
                    task_summary: Some("updated non-identity summary".to_string()),
                    sub_session_id: Some("child-b".to_string()),
                    scheduling_contract: Some(current_contract),
                    ..SessionTaskPatch::default()
                },
                generated_task_id: "unused".to_string(),
                now: Utc::now(),
            })
            .expect("identity-preserving claimed task patch");
        assert_eq!(
            claimed.task_plan.detailed_tasks[0].dispatch_claim,
            claimed_preimage.task_plan.detailed_tasks[0].dispatch_claim
        );
    }

    #[test]
    fn child_dispatch_claim_rejects_plan_drift_without_mutation() {
        let task_plan = TaskPlan {
            plan_summary: "plan".to_string(),
            detailed_tasks: vec![TaskStep {
                task_id: "task-b".to_string(),
                start_condition: StartCondition::SessionIdle,
                scheduling_contract: Some(scheduling_contract("task-b", 'a', vec![], vec![])),
                ..TaskStep::default()
            }],
        };
        let mut aggregate = SessionAggregate::new("session-fixed".to_string());
        aggregate.apply(&SessionEvent::SessionCreated {
            task_plan: task_plan.clone(),
        });
        let preimage = aggregate.clone();
        let claim = dispatch_claim(&aggregate.task_plan);
        let mut wrong_contract_digest = claim.clone();
        wrong_contract_digest.task_scheduling_contract_sha256 = "f".repeat(64);
        assert!(
            aggregate
                .execute(SessionCommand::ClaimScheduledChildTask {
                    expected_task_plan: task_plan.clone(),
                    claim: wrong_contract_digest,
                    now: Utc::now(),
                })
                .is_err()
        );
        assert_eq!(aggregate, preimage);

        let mut drifted = task_plan;
        drifted.plan_summary = "drifted".to_string();
        assert!(
            aggregate
                .execute(SessionCommand::ClaimScheduledChildTask {
                    expected_task_plan: drifted,
                    claim,
                    now: Utc::now(),
                })
                .is_err()
        );
        assert_eq!(aggregate, preimage);
    }

    #[test]
    fn replay_rejects_noncanonical_session_history() {
        let created = SessionEvent::SessionCreated {
            task_plan: TaskPlan::default(),
        };
        assert!(SessionAggregate::replay("session-fixed".to_string(), []).is_err());
        assert!(
            SessionAggregate::replay(
                "session-fixed".to_string(),
                [SessionEvent::RuntimeStarted {
                    runtime_id: "runtime-fixed".to_string(),
                    state: SessionState::Running,
                }],
            )
            .is_err()
        );
        assert!(
            SessionAggregate::replay(
                "session-fixed".to_string(),
                [created.clone(), created.clone()],
            )
            .is_err()
        );
        assert!(
            SessionAggregate::replay(
                "session-fixed".to_string(),
                [
                    created,
                    SessionEvent::RuntimeStarted {
                        runtime_id: "runtime-fixed".to_string(),
                        state: SessionState::Paused,
                    },
                ],
            )
            .is_err()
        );
    }

    #[test]
    fn replay_accepts_creation_variants_retry_and_scheduled_claim() {
        for first in [
            SessionEvent::SessionForked {
                parent_id: "parent-fixed".to_string(),
            },
            SessionEvent::ChildSessionRegistered {
                parent_id: "parent-fixed".to_string(),
                state: SessionState::Running,
            },
        ] {
            SessionAggregate::replay("session-fixed".to_string(), [first])
                .expect("canonical creation variant should replay");
        }

        let child_events = [
            SessionEvent::SessionCreated {
                task_plan: TaskPlan::default(),
            },
            SessionEvent::ChildSessionRegistered {
                parent_id: "parent-fixed".to_string(),
                state: SessionState::Running,
            },
        ];
        let child = SessionAggregate::replay("session-fixed".to_string(), child_events)
            .expect("existing session may register as a child");
        assert_eq!(child.parent_id.as_deref(), Some("parent-fixed"));

        let mut retry = SessionAggregate::new("session-fixed".to_string());
        let mut retry_events = vec![
            retry
                .execute(SessionCommand::CreateSession {
                    task_plan: TaskPlan::default(),
                })
                .expect("create session"),
            retry
                .execute(SessionCommand::RuntimeStarted {
                    runtime_id: "runtime-first".to_string(),
                })
                .expect("start runtime"),
            retry
                .execute(SessionCommand::RuntimeFailed {
                    runtime_id: "runtime-first".to_string(),
                })
                .expect("fail runtime"),
        ];
        retry_events.push(
            retry
                .execute(SessionCommand::RuntimeRetried {
                    runtime_id: "runtime-retry".to_string(),
                    fallback_from_id: "runtime-first".to_string(),
                })
                .expect("retry runtime"),
        );
        assert_eq!(
            SessionAggregate::replay("session-fixed".to_string(), retry_events)
                .expect("retry history should replay"),
            retry
        );

        let now = Utc::now();
        let task_plan = TaskPlan {
            plan_summary: "Scheduled plan".to_string(),
            detailed_tasks: vec![TaskStep {
                task_id: "task-fixed".to_string(),
                start_at: now,
                start_condition: StartCondition::PollingTask,
                poll_interval: PollInterval {
                    m: 5,
                    ..PollInterval::default()
                },
                task_summary: "Run now".to_string(),
                ..TaskStep::default()
            }],
        };
        let mut scheduled = SessionAggregate::new("session-fixed".to_string());
        let scheduled_events = vec![
            scheduled
                .execute(SessionCommand::CreateSession { task_plan })
                .expect("create scheduled session"),
            scheduled
                .execute(SessionCommand::StartScheduledTask {
                    task_id: "task-fixed".to_string(),
                    task_summary: "Run now".to_string(),
                    start_condition: StartCondition::PollingTask,
                    now,
                })
                .expect("claim scheduled task"),
        ];
        assert_eq!(
            SessionAggregate::replay("session-fixed".to_string(), scheduled_events)
                .expect("scheduled history should replay"),
            scheduled
        );
    }

    #[test]
    fn transition_matrix_matches_the_reference_session() {
        use SessionState::*;

        let states = [
            Created,
            Running,
            Paused,
            Completed,
            Failed,
            Cancelled,
            Interrupted,
        ];
        for from in states {
            for to in states {
                let expected = matches!(
                    (from, to),
                    (Created, Created | Running | Cancelled)
                        | (
                            Running,
                            Running | Paused | Completed | Failed | Cancelled | Interrupted
                        )
                        | (Paused, Paused | Running | Cancelled | Failed | Interrupted)
                        | (Completed, Completed | Created | Running)
                );
                assert_eq!(
                    from.can_transition_to(to),
                    expected,
                    "unexpected SessionState transition for {from:?} -> {to:?}"
                );
            }
        }
    }

    #[test]
    fn serde_accepts_only_the_current_canonical_names() {
        let task_plan = TaskPlan::default();
        let task_plan_json = serde_json::to_value(&task_plan).expect("serialize task plan");
        assert_eq!(
            serde_json::from_value::<TaskPlan>(task_plan_json).expect("deserialize task plan"),
            task_plan
        );
        assert!(serde_json::from_value::<TaskPlan>(serde_json::json!([])).is_err());

        for (state, encoded) in [
            (SessionState::Created, "\"created\""),
            (SessionState::Running, "\"running\""),
            (SessionState::Paused, "\"paused\""),
            (SessionState::Completed, "\"completed\""),
            (SessionState::Failed, "\"failed\""),
            (SessionState::Cancelled, "\"cancelled\""),
            (SessionState::Interrupted, "\"interrupted\""),
        ] {
            assert_eq!(serde_json::to_string(&state).expect("serialize"), encoded);
            assert_eq!(
                serde_json::from_str::<SessionState>(encoded).expect("deserialize"),
                state
            );
        }

        for invalid in ["\"Created\"", "\"busy\"", "\"cancelled_by_user\""] {
            assert!(serde_json::from_str::<SessionState>(invalid).is_err());
        }
    }

    #[test]
    fn aggregate_command_covers_the_complete_transition_table() {
        use SessionState::*;

        let states = [
            Created,
            Running,
            Paused,
            Completed,
            Failed,
            Cancelled,
            Interrupted,
        ];
        for previous in states {
            for next in states {
                let mut aggregate = aggregate_in_state(previous);
                let result = aggregate.execute(SessionCommand::ApplyRuntimeState { state: next });
                assert_eq!(result.is_ok(), previous.can_transition_to(next));
                if previous.can_transition_to(next) {
                    assert_eq!(aggregate.state, next);
                    assert_eq!(
                        result.expect("valid transition event"),
                        SessionEvent::RuntimeStateApplied { state: next }
                    );
                } else {
                    assert_eq!(aggregate.state, previous);
                }
            }
        }
    }

    #[test]
    fn active_runtime_state_application_accepts_live_and_terminal_transitions() {
        for terminal in [
            SessionState::Completed,
            SessionState::Failed,
            SessionState::Cancelled,
            SessionState::Interrupted,
        ] {
            let mut aggregate = SessionAggregate::new("session-fixed".to_string());
            aggregate
                .execute(SessionCommand::RuntimeStarted {
                    runtime_id: "runtime-fixed".to_string(),
                })
                .expect("runtime should start");

            for next in [SessionState::Paused, SessionState::Running] {
                aggregate
                    .execute(SessionCommand::ApplyRuntimeState { state: next })
                    .expect("active runtime may pause or resume");
                assert_eq!(aggregate.state, next);
                assert_eq!(
                    aggregate.active_runtime_id.as_deref(),
                    Some("runtime-fixed")
                );
            }

            aggregate
                .execute(SessionCommand::ApplyRuntimeState { state: terminal })
                .expect("active runtime may apply a legal terminal state");
            assert_eq!(aggregate.state, terminal);
            assert_eq!(aggregate.active_runtime_id, None);
        }

        let mut aggregate = SessionAggregate::new("session-fixed".to_string());
        aggregate
            .execute(SessionCommand::RuntimeStarted {
                runtime_id: "runtime-fixed".to_string(),
            })
            .expect("runtime should start");
        assert!(
            aggregate
                .decide(SessionCommand::ApplyRuntimeState {
                    state: SessionState::Created,
                })
                .is_err()
        );
    }

    #[test]
    fn session_protocol_is_strict_and_projection_is_derived() {
        let aggregate = SessionAggregate::new("session-fixed".to_string());
        assert_eq!(
            aggregate.query(SessionQuery::Lifecycle),
            SessionProjection {
                session_id: "session-fixed".to_string(),
                state: SessionState::Created,
                parent_id: None,
                task_plan: TaskPlan::default(),
                pending_user_inputs: Vec::new(),
                cancelled: false,
                runtime_ids: Vec::new(),
                active_runtime_id: None,
            }
        );
        let retry = SessionCommand::RuntimeRetried {
            runtime_id: "runtime-retry".to_string(),
            fallback_from_id: "runtime-failed".to_string(),
        };
        let retry_json = serde_json::to_value(&retry).expect("serialize runtime retry command");
        assert_eq!(
            retry_json,
            serde_json::json!({
                "command": "runtime_retried",
                "runtime_id": "runtime-retry",
                "fallback_from_id": "runtime-failed"
            })
        );
        assert_eq!(
            serde_json::from_value::<SessionCommand>(retry_json)
                .expect("deserialize runtime retry command"),
            retry
        );
        assert!(
            serde_json::from_value::<SessionCommand>(serde_json::json!({
                "command": "runtime_retried",
                "runtime_id": "runtime-retry"
            }))
            .is_err()
        );
        assert!(
            serde_json::from_str::<SessionCommand>(
                r#"{"command":"apply_runtime_state","state":"running","extra":true}"#
            )
            .is_err()
        );
        assert!(
            serde_json::from_str::<SessionProjection>(
                r#"{"session_id":"session-fixed","state":"created","extra":true}"#
            )
            .is_err()
        );
    }

    #[test]
    fn prepare_user_turn_preserves_live_states_and_reopens_terminal_states() {
        for (previous, expected) in [
            (SessionState::Created, SessionState::Created),
            (SessionState::Running, SessionState::Running),
            (SessionState::Paused, SessionState::Paused),
            (SessionState::Completed, SessionState::Created),
            (SessionState::Failed, SessionState::Created),
            (SessionState::Cancelled, SessionState::Created),
            (SessionState::Interrupted, SessionState::Created),
        ] {
            let mut aggregate = aggregate_in_state(previous);
            let event = aggregate
                .execute(SessionCommand::SubmitUserInput)
                .expect("preparing a user turn is always valid");
            assert_eq!(aggregate.state, expected);
            assert_eq!(event, SessionEvent::UserInputAccepted { state: expected });
        }
    }

    #[test]
    fn interrupt_cancel_and_child_commands_cover_business_boundaries() {
        let mut aggregate = SessionAggregate::new("session-fixed".to_string());
        aggregate
            .execute(SessionCommand::InterruptSession)
            .expect("created session can be interrupted");
        assert_eq!(aggregate.state, SessionState::Interrupted);

        aggregate
            .execute(SessionCommand::SubmitUserInput)
            .expect("interrupted session should reopen");
        aggregate
            .execute(SessionCommand::RegisterChildSession {
                parent_id: "parent-fixed".to_string(),
            })
            .expect("reopened child session should start");
        assert_eq!(aggregate.state, SessionState::Running);
        assert_eq!(aggregate.parent_id.as_deref(), Some("parent-fixed"));

        aggregate
            .execute(SessionCommand::CancelSession)
            .expect("active session can be cancelled");
        assert_eq!(aggregate.state, SessionState::Cancelled);
        assert!(aggregate.cancelled);

        let mut completed = aggregate_in_state(SessionState::Completed);
        completed.task_plan.detailed_tasks.push(TaskStep {
            task_id: "completed-task".to_string(),
            status: PlanStatus::Doing,
            ..TaskStep::default()
        });
        let completed_projection = completed.query(SessionQuery::Lifecycle);
        completed
            .execute(SessionCommand::InterruptSession)
            .expect("duplicate interruption after completion is harmless");
        assert_eq!(
            completed.query(SessionQuery::Lifecycle),
            completed_projection
        );
    }

    #[test]
    fn task_patch_and_pending_input_commands_update_one_projection() {
        let now = chrono::Utc::now();
        let mut aggregate = SessionAggregate::new("session-fixed".to_string());
        aggregate
            .execute(SessionCommand::ApplyTaskPatch {
                patch: SessionTaskPatch {
                    task_summary: Some("Ship Phase 2".to_string()),
                    poll_interval: Some(PollInterval {
                        m: 5,
                        ..PollInterval::default()
                    }),
                    status: Some(PlanStatus::Question),
                    ..SessionTaskPatch::default()
                },
                generated_task_id: "task-fixed".to_string(),
                now,
            })
            .expect("task patch should apply");
        aggregate
            .execute(SessionCommand::StartUserTurn)
            .expect("user turn should start before an input is queued");
        aggregate
            .execute(SessionCommand::QueueUserInputWhileBusy {
                input: "continue".to_string(),
            })
            .expect("user input should queue");

        let projection = aggregate.query(SessionQuery::Lifecycle);
        assert_eq!(projection.task_plan.plan_summary, "Ship Phase 2");
        assert_eq!(projection.task_plan.detailed_tasks.len(), 1);
        assert_eq!(
            projection.task_plan.detailed_tasks[0].start_condition,
            StartCondition::PollingTask
        );
        assert_eq!(projection.pending_user_inputs, vec!["continue"]);
    }

    #[test]
    fn user_turn_queue_consume_and_cancel_are_one_canonical_lifecycle() {
        let mut aggregate = SessionAggregate::new("session-fixed".to_string());
        aggregate
            .execute(SessionCommand::CancelSession)
            .expect("created session should cancel");
        assert!(aggregate.cancelled);

        aggregate
            .execute(SessionCommand::StartUserTurn)
            .expect("new input should atomically reopen and start a cancelled session");
        assert_eq!(aggregate.state, SessionState::Running);
        assert!(!aggregate.cancelled);

        for input in [" first ", "second"] {
            aggregate
                .execute(SessionCommand::QueueUserInputWhileBusy {
                    input: input.to_string(),
                })
                .expect("busy input should queue");
        }
        let consumed = aggregate
            .execute(SessionCommand::ConsumeQueuedUserInputs)
            .expect("queued inputs should be consumed atomically");
        assert_eq!(
            consumed,
            SessionEvent::QueuedUserInputsConsumed {
                inputs: vec!["first".to_string(), "second".to_string()]
            }
        );
        assert!(aggregate.pending_user_inputs.is_empty());

        aggregate
            .execute(SessionCommand::QueueUserInputWhileBusy {
                input: "discard on cancel".to_string(),
            })
            .expect("busy input should queue");
        aggregate
            .execute(SessionCommand::CancelSession)
            .expect("running session should cancel");
        assert!(aggregate.pending_user_inputs.is_empty());
    }

    #[test]
    fn busy_queue_and_runtime_result_commands_reject_invalid_states() {
        let mut aggregate = SessionAggregate::new("session-fixed".to_string());
        assert!(
            aggregate
                .execute(SessionCommand::QueueUserInputWhileBusy {
                    input: "not busy".to_string()
                })
                .is_err()
        );

        aggregate
            .execute(SessionCommand::RuntimeStarted {
                runtime_id: "runtime-fixed".to_string(),
            })
            .expect("created session may start a runtime");
        assert_eq!(aggregate.state, SessionState::Running);
        assert_eq!(aggregate.runtime_ids, ["runtime-fixed"]);
        assert_eq!(
            aggregate.active_runtime_id.as_deref(),
            Some("runtime-fixed")
        );
        assert!(
            aggregate
                .execute(SessionCommand::RuntimeCompleted {
                    runtime_id: "runtime-stale".to_string(),
                })
                .is_err()
        );
        assert_eq!(aggregate.state, SessionState::Running);
        assert_eq!(
            aggregate.active_runtime_id.as_deref(),
            Some("runtime-fixed")
        );
        aggregate
            .execute(SessionCommand::RuntimeCompleted {
                runtime_id: "runtime-fixed".to_string(),
            })
            .expect("running runtime may complete");
        assert_eq!(aggregate.state, SessionState::Completed);
        assert_eq!(aggregate.active_runtime_id, None);
        assert!(
            aggregate
                .execute(SessionCommand::RuntimeFailed {
                    runtime_id: "runtime-fixed".to_string(),
                })
                .is_err()
        );

        let mut failed = aggregate_in_state(SessionState::Running);
        failed
            .execute(SessionCommand::RuntimeStarted {
                runtime_id: "runtime-failed".to_string(),
            })
            .expect("runtime should start");
        failed
            .execute(SessionCommand::RuntimeFailed {
                runtime_id: "runtime-failed".to_string(),
            })
            .expect("active runtime failure should apply");
        assert_eq!(failed.state, SessionState::Failed);
        assert_eq!(failed.active_runtime_id, None);
        assert!(
            failed
                .decide(SessionCommand::RuntimeStarted {
                    runtime_id: "runtime-unrelated".to_string(),
                })
                .is_err()
        );
        assert!(
            failed
                .decide(SessionCommand::RuntimeRetried {
                    runtime_id: "runtime-retry".to_string(),
                    fallback_from_id: "runtime-stale".to_string(),
                })
                .is_err()
        );
        assert!(
            aggregate
                .decide(SessionCommand::RuntimeRetried {
                    runtime_id: "runtime-retry".to_string(),
                    fallback_from_id: "runtime-fixed".to_string(),
                })
                .is_err()
        );
        assert!(
            failed
                .decide(SessionCommand::RuntimeRetried {
                    runtime_id: "runtime-failed".to_string(),
                    fallback_from_id: "runtime-failed".to_string(),
                })
                .is_err()
        );
        failed
            .execute(SessionCommand::RuntimeRetried {
                runtime_id: "runtime-retry".to_string(),
                fallback_from_id: "runtime-failed".to_string(),
            })
            .expect("failed session may start a retry of its latest runtime");
        assert_eq!(failed.state, SessionState::Running);
        assert_eq!(
            failed.runtime_ids,
            ["runtime-failed".to_string(), "runtime-retry".to_string()]
        );
        assert_eq!(failed.active_runtime_id.as_deref(), Some("runtime-retry"));

        let mut cancelled = aggregate_in_state(SessionState::Running);
        cancelled
            .execute(SessionCommand::RuntimeStarted {
                runtime_id: "runtime-cancelled".to_string(),
            })
            .expect("runtime should start before its terminal callback");
        cancelled
            .execute(SessionCommand::RuntimeCancelled {
                runtime_id: "runtime-cancelled".to_string(),
            })
            .expect("active runtime cancellation should apply");
        assert_eq!(cancelled.state, SessionState::Cancelled);
        assert_eq!(cancelled.runtime_ids, ["runtime-cancelled"]);
        assert_eq!(cancelled.active_runtime_id, None);
        assert!(cancelled.cancelled);
    }

    #[test]
    fn scheduler_claim_is_atomic_with_task_and_lifecycle_state() {
        let now = chrono::Utc::now();
        let mut aggregate = SessionAggregate::new("session-fixed".to_string());
        aggregate.task_plan = TaskPlan {
            plan_summary: "Scheduled work".to_string(),
            detailed_tasks: vec![TaskStep {
                task_id: "task-fixed".to_string(),
                start_at: now,
                start_condition: StartCondition::ScheduledTask,
                status: PlanStatus::Todo,
                task_summary: "Run now".to_string(),
                ..TaskStep::default()
            }],
        };

        let event = aggregate
            .execute(SessionCommand::StartScheduledTask {
                task_id: "task-fixed".to_string(),
                task_summary: "Run now".to_string(),
                start_condition: StartCondition::ScheduledTask,
                now,
            })
            .expect("due task should be claimed");

        assert!(matches!(event, SessionEvent::ScheduledTaskClaimed { .. }));
        assert_eq!(aggregate.state, SessionState::Running);
        assert_eq!(
            aggregate.task_plan.detailed_tasks[0].status,
            PlanStatus::Doing
        );
    }

    #[test]
    fn scheduler_claim_rejects_stale_task_preconditions_without_mutation() {
        let now = chrono::Utc::now();
        let mut aggregate = SessionAggregate::new("session-fixed".to_string());
        aggregate.task_plan = TaskPlan {
            plan_summary: "Scheduled work".to_string(),
            detailed_tasks: vec![TaskStep {
                task_id: "task-due".to_string(),
                start_at: now,
                start_condition: StartCondition::ScheduledTask,
                status: PlanStatus::Todo,
                task_summary: "Run now".to_string(),
                ..TaskStep::default()
            }],
        };
        let before = aggregate.clone();

        assert!(
            aggregate
                .execute(SessionCommand::StartScheduledTask {
                    task_id: "task-stale".to_string(),
                    task_summary: "Run now".to_string(),
                    start_condition: StartCondition::ScheduledTask,
                    now,
                })
                .is_err()
        );
        assert_eq!(aggregate, before);

        assert!(
            aggregate
                .execute(SessionCommand::StartScheduledTask {
                    task_id: "task-due".to_string(),
                    task_summary: "Stale summary".to_string(),
                    start_condition: StartCondition::ScheduledTask,
                    now,
                })
                .is_err()
        );
        assert_eq!(aggregate, before);

        assert!(
            aggregate
                .execute(SessionCommand::StartScheduledTask {
                    task_id: "task-due".to_string(),
                    task_summary: "Run now".to_string(),
                    start_condition: StartCondition::PollingTask,
                    now,
                })
                .is_err()
        );
        assert_eq!(aggregate, before);
    }
}
