#![forbid(unsafe_code)]

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeSet, HashMap};
use std::path::Path;

use lifecycle::SessionState;

mod task_context_v1;
pub use task_context_v1::{
    canonical_json as task_context_canonical_json_v1,
    semantic_sha256 as task_context_semantic_sha256_v1,
    PRESENTATION_VERSION as TASK_CONTEXT_PRESENTATION_VERSION,
};

pub const WORKER_KIND_CALL: &str = "call";
pub const WORKER_KIND_HEALTH_CHECK: &str = "health_check";
pub const DEFAULT_MAXIMUM_RUNTIME_LLM_TURNS: u64 = 256;
pub const MAXIMUM_RUNTIME_LLM_TURN_OPTIONS: [u64; 5] = [64, 128, 256, 1_080, 2_560];
pub const DEFAULT_MAXIMUM_PARALLEL_RUNTIME_WORKERS: usize = 24;
pub const MAXIMUM_PARALLEL_RUNTIME_WORKER_OPTIONS: [usize; 5] = [6, 12, 24, 48, 128];
pub const TASK_CONTEXT_CAPSULE_SCHEMA_VERSION: &str = "task_context_capsule_v1";
pub const MAXIMUM_TASK_CONTEXT_SUMMARY_CHARS: usize = 32_000;
pub const MAXIMUM_TASK_CONTEXT_EVIDENCE_REFS: usize = 64;
pub const NATIVE_CODEX_TASK_DELTA_SCHEMA_VERSION: &str = "tura_native_codex_task_delta_v1";
pub const NATIVE_CODEX_EXECUTION_BINDING_SCHEMA_VERSION: &str =
    "tura_native_codex_execution_binding_v1";
pub const MAXIMUM_NATIVE_CODEX_TASK_DELTA_BYTES: usize = 256 * 1024;
pub const SESSION_SERVICE_TIER_ENV: &str = "TURA_SESSION_SERVICE_TIER";
pub const NATIVE_CODEX_TERMINAL_ENVELOPE_SCHEMA_VERSION: &str =
    "tura_native_codex_terminal_envelope_v2";

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ModelServiceTier {
    Default,
    Priority,
    Ultrafast,
}

impl ModelServiceTier {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Default => "default",
            Self::Priority => "priority",
            Self::Ultrafast => "ultrafast",
        }
    }
}

impl std::str::FromStr for ModelServiceTier {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value.trim().to_ascii_lowercase().as_str() {
            "default" => Ok(Self::Default),
            "priority" => Ok(Self::Priority),
            "ultrafast" => Ok(Self::Ultrafast),
            other => Err(format!("TURA_SESSION_SERVICE_TIER_INVALID:{other}")),
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum NativeCodexTerminalState {
    Completed,
    Failed,
    Interrupted,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct NativeCodexToolObservation {
    pub item_id: String,
    pub item_type: String,
    pub tool_name: String,
    pub effect_id: String,
    pub input_sha256: String,
    pub output_sha256: String,
    pub status: String,
    pub is_error: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exit_code: Option<i32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct NativeCodexTerminalEnvelope {
    pub schema_version: String,
    pub task_id: String,
    pub execution_id: String,
    pub lease_id: String,
    pub execution_profile_sha256: String,
    pub execution_binding_sha256: String,
    pub task_context_capsule_sha256: String,
    pub task_delta_sha256: String,
    pub provider_input_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub worker_thread_id: Option<String>,
    pub terminal_state: NativeCodexTerminalState,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub final_text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub final_text_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tool_observations: Vec<NativeCodexToolObservation>,
    pub event_stream_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub process_exit_code: Option<i32>,
    pub process_reaped: bool,
    pub terminal: bool,
    pub execution_lease_active: bool,
    pub ephemeral: bool,
    pub private_codex_state_write_count: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub usage: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl NativeCodexTerminalEnvelope {
    pub fn validate_shape(&self) -> Result<(), String> {
        if self.schema_version != NATIVE_CODEX_TERMINAL_ENVELOPE_SCHEMA_VERSION {
            return Err("NATIVE_CODEX_TERMINAL_SCHEMA_UNSUPPORTED".to_string());
        }
        for (name, value) in [
            ("task_id", self.task_id.as_str()),
            ("execution_id", self.execution_id.as_str()),
            ("lease_id", self.lease_id.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(format!("NATIVE_CODEX_TERMINAL_IDENTITY_MISSING:{name}"));
            }
        }
        for digest in [
            &self.execution_profile_sha256,
            &self.execution_binding_sha256,
            &self.task_context_capsule_sha256,
            &self.task_delta_sha256,
            &self.provider_input_sha256,
        ] {
            if !is_lower_sha256(digest) {
                return Err("NATIVE_CODEX_CONTEXT_IDENTITY_INVALID".to_string());
            }
        }
        if !self.terminal || self.execution_lease_active || !self.process_reaped {
            return Err("NATIVE_CODEX_TERMINAL_LIFECYCLE_OPEN".to_string());
        }
        if !self.ephemeral || self.private_codex_state_write_count != 0 {
            return Err("NATIVE_CODEX_PRIVATE_STATE_BOUNDARY_VIOLATION".to_string());
        }
        if !is_lower_sha256(&self.event_stream_sha256) {
            return Err("NATIVE_CODEX_EVENT_STREAM_IDENTITY_INVALID".to_string());
        }
        match (&self.final_text, &self.final_text_sha256) {
            (Some(text), Some(digest))
                if format!("{:x}", Sha256::digest(text.as_bytes())) == *digest => {}
            (None, None) if self.terminal_state != NativeCodexTerminalState::Completed => {}
            _ => return Err("NATIVE_CODEX_FINAL_TEXT_IDENTITY_INVALID".to_string()),
        }
        if self.terminal_state == NativeCodexTerminalState::Completed
            && (self.process_exit_code != Some(0) || self.final_text.is_none())
        {
            return Err("NATIVE_CODEX_COMPLETED_TERMINAL_INVALID".to_string());
        }
        for tool in &self.tool_observations {
            if tool.item_id.trim().is_empty()
                || tool.item_type.trim().is_empty()
                || tool.tool_name.trim().is_empty()
                || tool.effect_id.trim().is_empty()
                || !is_lower_sha256(&tool.input_sha256)
                || !is_lower_sha256(&tool.output_sha256)
            {
                return Err("NATIVE_CODEX_TOOL_OBSERVATION_INVALID".to_string());
            }
        }
        Ok(())
    }
}

pub fn maximum_runtime_llm_turns(value: Option<u64>) -> u64 {
    value
        .filter(|value| MAXIMUM_RUNTIME_LLM_TURN_OPTIONS.contains(value))
        .unwrap_or(DEFAULT_MAXIMUM_RUNTIME_LLM_TURNS)
}

pub fn maximum_parallel_runtime_workers(value: Option<usize>) -> usize {
    value
        .filter(|value| MAXIMUM_PARALLEL_RUNTIME_WORKER_OPTIONS.contains(value))
        .unwrap_or(DEFAULT_MAXIMUM_PARALLEL_RUNTIME_WORKERS)
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CallContext {
    pub request_id: String,
    pub method: String,
    pub path: String,
    pub input: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct LifecycleExecutionContext {
    pub transaction_id: String,
    pub commander_session_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_mission_revision_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delegated_input_sha256: Option<String>,
    #[serde(default)]
    pub task_id: Option<String>,
    #[serde(default)]
    pub goal_id: Option<String>,
    #[serde(default)]
    pub operator_override: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub commander_continuation: Option<CommanderContinuationBinding>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CommanderContinuationBinding {
    pub target_thread_id: String,
    pub requested_action: String,
    pub continuation_request_id: String,
    pub child_session_id: String,
    pub child_transaction_id: String,
    pub child_runtime_id: String,
    pub callback_payload_sha256: String,
    pub effect_identity_sha256: String,
    pub pre_revision_sha256: String,
}

impl CommanderContinuationBinding {
    pub fn validate(&self) -> Result<(), String> {
        for (name, value) in [
            ("target_thread_id", self.target_thread_id.as_str()),
            ("requested_action", self.requested_action.as_str()),
            (
                "continuation_request_id",
                self.continuation_request_id.as_str(),
            ),
            ("child_session_id", self.child_session_id.as_str()),
            ("child_transaction_id", self.child_transaction_id.as_str()),
            ("child_runtime_id", self.child_runtime_id.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(format!("COMMANDER_CONTINUATION_IDENTITY_MISSING:{name}"));
            }
        }
        if !matches!(
            self.requested_action.as_str(),
            "MISSION_VERIFICATION" | "ROUTE_SELECTION"
        ) {
            return Err("COMMANDER_CONTINUATION_ACTION_INVALID".to_string());
        }
        for (name, value) in [
            (
                "callback_payload_sha256",
                self.callback_payload_sha256.as_str(),
            ),
            (
                "effect_identity_sha256",
                self.effect_identity_sha256.as_str(),
            ),
            ("pre_revision_sha256", self.pre_revision_sha256.as_str()),
        ] {
            if !is_lower_sha256(value) {
                return Err(format!("COMMANDER_CONTINUATION_IDENTITY_INVALID:{name}"));
            }
        }
        Ok(())
    }
}

pub const COMMANDER_CONVERGENCE_PROOF_SCHEMA_VERSION: &str = "tura_commander_convergence_proof_v1";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CommanderConvergenceProof {
    pub schema_version: String,
    pub request_id: String,
    pub callback_payload_sha256: String,
    pub effect_identity_sha256: String,
    pub child_session_id: String,
    pub child_transaction_id: String,
    pub child_runtime_id: String,
    pub requested_action: String,
    pub target_thread_id: String,
    pub pre_revision_sha256: String,
    pub post_revision_sha256: String,
    pub target_turn_id: String,
    pub final_assistant_sha256: String,
}

impl CommanderConvergenceProof {
    pub fn validate_shape(&self) -> Result<(), String> {
        if self.schema_version != COMMANDER_CONVERGENCE_PROOF_SCHEMA_VERSION {
            return Err("COMMANDER_CONVERGENCE_PROOF_SCHEMA_UNSUPPORTED".to_string());
        }
        let binding = CommanderContinuationBinding {
            target_thread_id: self.target_thread_id.clone(),
            requested_action: self.requested_action.clone(),
            continuation_request_id: self.request_id.clone(),
            child_session_id: self.child_session_id.clone(),
            child_transaction_id: self.child_transaction_id.clone(),
            child_runtime_id: self.child_runtime_id.clone(),
            callback_payload_sha256: self.callback_payload_sha256.clone(),
            effect_identity_sha256: self.effect_identity_sha256.clone(),
            pre_revision_sha256: self.pre_revision_sha256.clone(),
        };
        binding.validate()?;
        if self.target_turn_id.trim().is_empty() {
            return Err("COMMANDER_CONVERGENCE_PROOF_IDENTITY_MISSING:target_turn_id".to_string());
        }
        for (name, value) in [
            ("post_revision_sha256", self.post_revision_sha256.as_str()),
            (
                "final_assistant_sha256",
                self.final_assistant_sha256.as_str(),
            ),
        ] {
            if !is_lower_sha256(value) {
                return Err(format!(
                    "COMMANDER_CONVERGENCE_PROOF_IDENTITY_INVALID:{name}"
                ));
            }
        }
        if self.post_revision_sha256 == self.pre_revision_sha256 {
            return Err("COMMANDER_CONVERGENCE_PROOF_REVISION_DID_NOT_ADVANCE".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TaskContextMission {
    pub mission_id: String,
    #[serde(default, deserialize_with = "task_context_v1::non_null_optional_string", skip_serializing_if = "Option::is_none")]
    pub task_id: Option<String>,
    pub mode: String,
    pub current_predicate: String,
    pub objective: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TaskContextEvidenceReference {
    pub id: String,
    pub kind: String,
    #[serde(default, deserialize_with = "task_context_v1::non_null_optional_string", skip_serializing_if = "Option::is_none")]
    pub sha256: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct NativeCodexTaskDelta {
    pub schema_version: String,
    pub mission_id: String,
    pub mission_revision_sha256: String,
    pub task_id: String,
    pub current_predicate: String,
    pub instruction: String,
    pub semantic_sha256: String,
}

impl NativeCodexTaskDelta {
    pub fn from_value(value: Value) -> Result<Self, String> {
        let delta: Self = serde_json::from_value(value)
            .map_err(|error| format!("NATIVE_CODEX_TASK_DELTA_MALFORMED:{error}"))?;
        delta.validate()?;
        Ok(delta)
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != NATIVE_CODEX_TASK_DELTA_SCHEMA_VERSION {
            return Err("NATIVE_CODEX_TASK_DELTA_SCHEMA_UNSUPPORTED".to_string());
        }
        for (field, value) in [
            ("mission_id", self.mission_id.as_str()),
            ("task_id", self.task_id.as_str()),
            ("current_predicate", self.current_predicate.as_str()),
            ("instruction", self.instruction.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(format!("NATIVE_CODEX_TASK_DELTA_FIELD_MISSING:{field}"));
            }
        }
        if !is_lower_sha256(&self.mission_revision_sha256) {
            return Err("NATIVE_CODEX_TASK_DELTA_REVISION_INVALID".to_string());
        }
        if self.instruction.len() > MAXIMUM_NATIVE_CODEX_TASK_DELTA_BYTES {
            return Err("NATIVE_CODEX_TASK_DELTA_TOO_LARGE".to_string());
        }
        for reserved in [
            "<skills_instructions>",
            "<permissions instructions>",
            "<apps_instructions>",
            "<plugins_instructions>",
            "<environment_context>",
            "# AGENTS.md instructions",
        ] {
            if self.instruction.contains(reserved) {
                return Err("NATIVE_CODEX_TASK_DELTA_RESERVED_CONTEXT_MARKER".to_string());
            }
        }
        if !is_lower_sha256(&self.semantic_sha256) {
            return Err("NATIVE_CODEX_TASK_DELTA_DIGEST_INVALID".to_string());
        }
        let mut payload = serde_json::to_value(self)
            .map_err(|error| format!("NATIVE_CODEX_TASK_DELTA_MALFORMED:{error}"))?;
        payload
            .as_object_mut()
            .expect("NativeCodexTaskDelta serializes as an object")
            .remove("semantic_sha256");
        let expected = semantic_sha256(&payload);
        if self.semantic_sha256 != expected {
            return Err(format!(
                "NATIVE_CODEX_TASK_DELTA_DIGEST_MISMATCH: expected {expected}, got {}",
                self.semantic_sha256
            ));
        }
        Ok(())
    }

    pub fn bind_capsule(&self, capsule: &TaskContextCapsule) -> Result<(), String> {
        self.validate()?;
        capsule.validate()?;
        if self.mission_id != capsule.mission.mission_id
            || capsule.mission.task_id.as_deref() != Some(self.task_id.as_str())
            || self.current_predicate != capsule.mission.current_predicate
        {
            return Err("NATIVE_CODEX_TASK_DELTA_CAPSULE_IDENTITY_MISMATCH".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum NativeCodexSandbox {
    ReadOnly,
    WorkspaceWrite,
}

impl NativeCodexSandbox {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ReadOnly => "read-only",
            Self::WorkspaceWrite => "workspace-write",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct NativeCodexExecutionBinding {
    pub schema_version: String,
    pub execution_profile_sha256: String,
    pub task_delta: NativeCodexTaskDelta,
    pub codex_executable: String,
    pub codex_executable_sha256: String,
    pub command_graph_executable: String,
    pub command_graph_executable_sha256: String,
    pub command_graph_allowed_commands: Vec<String>,
    pub sandbox: NativeCodexSandbox,
    pub timeout_ms: u64,
    pub semantic_sha256: String,
}

impl NativeCodexExecutionBinding {
    pub fn from_value(value: Value) -> Result<Self, String> {
        let binding: Self = serde_json::from_value(value)
            .map_err(|error| format!("NATIVE_CODEX_EXECUTION_BINDING_MALFORMED:{error}"))?;
        binding.validate()?;
        Ok(binding)
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != NATIVE_CODEX_EXECUTION_BINDING_SCHEMA_VERSION {
            return Err("NATIVE_CODEX_EXECUTION_BINDING_SCHEMA_UNSUPPORTED".to_string());
        }
        self.task_delta.validate()?;
        for digest in [
            &self.execution_profile_sha256,
            &self.codex_executable_sha256,
            &self.command_graph_executable_sha256,
            &self.semantic_sha256,
        ] {
            if !is_lower_sha256(digest) {
                return Err("NATIVE_CODEX_EXECUTION_BINDING_DIGEST_INVALID".to_string());
            }
        }
        for (name, value) in [
            ("codex_executable", self.codex_executable.as_str()),
            (
                "command_graph_executable",
                self.command_graph_executable.as_str(),
            ),
        ] {
            if value.trim().is_empty() || value.trim() != value || !Path::new(value).is_absolute() {
                return Err(format!(
                    "NATIVE_CODEX_EXECUTION_BINDING_PATH_INVALID:{name}"
                ));
            }
        }
        if self.timeout_ms == 0 || self.timeout_ms > 24 * 60 * 60 * 1_000 {
            return Err("NATIVE_CODEX_EXECUTION_BINDING_TIMEOUT_INVALID".to_string());
        }
        if self.sandbox != NativeCodexSandbox::ReadOnly {
            return Err("NATIVE_CODEX_EXECUTION_BINDING_REQUIRES_READ_ONLY_SANDBOX".to_string());
        }
        if self.command_graph_allowed_commands.is_empty()
            || self
                .command_graph_allowed_commands
                .iter()
                .any(|command| command.trim().is_empty() || command.trim() != command)
        {
            return Err("NATIVE_CODEX_EXECUTION_BINDING_ALLOWLIST_INVALID".to_string());
        }
        let canonical = self
            .command_graph_allowed_commands
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect::<Vec<_>>();
        if canonical != self.command_graph_allowed_commands {
            return Err("NATIVE_CODEX_EXECUTION_BINDING_ALLOWLIST_NOT_CANONICAL".to_string());
        }
        let mut payload = serde_json::to_value(self)
            .map_err(|error| format!("NATIVE_CODEX_EXECUTION_BINDING_MALFORMED:{error}"))?;
        payload
            .as_object_mut()
            .expect("NativeCodexExecutionBinding serializes as an object")
            .remove("semantic_sha256");
        let expected = semantic_sha256(&payload);
        if self.semantic_sha256 != expected {
            return Err(format!(
                "NATIVE_CODEX_EXECUTION_BINDING_DIGEST_MISMATCH: expected {expected}, got {}",
                self.semantic_sha256
            ));
        }
        Ok(())
    }

    pub fn bind_authoritative_task(
        &self,
        capsule: &TaskContextCapsule,
        task_id: &str,
        mission_revision_sha256: &str,
        prompt: &str,
    ) -> Result<(), String> {
        self.validate()?;
        self.task_delta.bind_capsule(capsule)?;
        if self.task_delta.task_id != task_id
            || self.task_delta.mission_revision_sha256 != mission_revision_sha256
            || self.task_delta.instruction != prompt
        {
            return Err("NATIVE_CODEX_EXECUTION_BINDING_TASK_IDENTITY_MISMATCH".to_string());
        }
        if !capsule
            .evidence_refs
            .iter()
            .any(|reference| reference.sha256.as_deref() == Some(self.semantic_sha256.as_str()))
        {
            return Err("NATIVE_CODEX_EXECUTION_BINDING_NOT_IN_TASK_EVIDENCE".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TaskContextCapsule {
    pub schema_version: String,
    pub mission: TaskContextMission,
    pub context_summary: String,
    pub dcf_generation: Value,
    pub surface: Value,
    pub authority: Value,
    pub evidence_refs: Vec<TaskContextEvidenceReference>,
    pub focused_verifiers: Vec<Value>,
    pub jspace_semantic_sha256: String,
    pub semantic_sha256: String,
}

impl TaskContextCapsule {
    pub fn from_value(value: Value) -> Result<Self, String> {
        let capsule: Self = serde_json::from_value(value)
            .map_err(|error| format!("TASK_CONTEXT_CAPSULE_MALFORMED: {error}"))?;
        capsule.validate()?;
        Ok(capsule)
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != TASK_CONTEXT_CAPSULE_SCHEMA_VERSION {
            return Err(format!(
                "TASK_CONTEXT_SCHEMA_VERSION_UNSUPPORTED: expected {TASK_CONTEXT_CAPSULE_SCHEMA_VERSION}, got {}",
                self.schema_version
            ));
        }
        for (field, value) in [
            ("mission_id", self.mission.mission_id.as_str()),
            ("mode", self.mission.mode.as_str()),
            ("current_predicate", self.mission.current_predicate.as_str()),
            ("objective", self.mission.objective.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(format!("TASK_CONTEXT_MISSION_INVALID: {field} is empty"));
            }
        }
        if self
            .mission
            .task_id
            .as_deref()
            .is_some_and(|value| value.trim().is_empty())
        {
            return Err("TASK_CONTEXT_MISSION_INVALID: task_id is empty".to_string());
        }
        if self.context_summary.trim().is_empty() {
            return Err("TASK_CONTEXT_SUMMARY_MISSING: context_summary is empty".to_string());
        }
        if self.context_summary.chars().count() > MAXIMUM_TASK_CONTEXT_SUMMARY_CHARS {
            return Err(format!(
                "TASK_CONTEXT_SUMMARY_TOO_LARGE: context_summary exceeds {MAXIMUM_TASK_CONTEXT_SUMMARY_CHARS} characters"
            ));
        }
        if self.evidence_refs.len() > MAXIMUM_TASK_CONTEXT_EVIDENCE_REFS {
            return Err(format!(
                "TASK_CONTEXT_EVIDENCE_INVALID: evidence_refs exceeds {MAXIMUM_TASK_CONTEXT_EVIDENCE_REFS} entries"
            ));
        }
        for evidence in &self.evidence_refs {
            if evidence.id.trim().is_empty() || evidence.kind.trim().is_empty() {
                return Err(
                    "TASK_CONTEXT_EVIDENCE_INVALID: evidence id and kind must be non-empty"
                        .to_string(),
                );
            }
            if evidence
                .sha256
                .as_deref()
                .is_some_and(|digest| !is_lower_sha256(digest))
            {
                return Err(
                    "TASK_CONTEXT_EVIDENCE_INVALID: evidence sha256 must be lowercase SHA-256"
                        .to_string(),
                );
            }
        }
        if !is_lower_sha256(&self.jspace_semantic_sha256) {
            return Err(
                "TASK_CONTEXT_JSPACE_DIGEST_INVALID: jspace_semantic_sha256 must be lowercase SHA-256"
                    .to_string(),
            );
        }
        if !is_lower_sha256(&self.semantic_sha256) {
            return Err(
                "TASK_CONTEXT_SEMANTIC_DIGEST_INVALID: semantic_sha256 must be lowercase SHA-256"
                    .to_string(),
            );
        }
        let mut payload = serde_json::to_value(self)
            .map_err(|error| format!("TASK_CONTEXT_CAPSULE_MALFORMED: {error}"))?;
        payload
            .as_object_mut()
            .expect("TaskContextCapsule serializes as an object")
            .remove("semantic_sha256");
        // Python v1 capsules use ensure_ascii=True and Python float formatting.
        // Other artifact schemas retain their existing UTF-8 hash domain.
        let expected = task_context_v1::semantic_sha256(&payload)?;
        if self.semantic_sha256 != expected {
            return Err(format!(
                "TASK_CONTEXT_SEMANTIC_DIGEST_MISMATCH: expected {expected}, got {}",
                self.semantic_sha256
            ));
        }
        Ok(())
    }

    pub fn bind_jspace(&self, jspace_contract: Option<&Value>) -> Result<(), String> {
        let digest = jspace_contract
            .and_then(|contract| {
                contract
                    .get("authorization_semantic_sha256")
                    .or_else(|| contract.get("semantic_sha256"))
            })
            .and_then(Value::as_str)
            .ok_or_else(|| {
                "TASK_CONTEXT_JSPACE_BINDING_MISSING: capsule requires a J-Space contract"
                    .to_string()
            })?;
        if digest != self.jspace_semantic_sha256 {
            return Err(format!(
                "TASK_CONTEXT_JSPACE_BINDING_MISMATCH: capsule={} contract={digest}",
                self.jspace_semantic_sha256
            ));
        }
        Ok(())
    }

    pub fn provider_context(&self) -> String {
        // Presentation does not replace the original capsule or grant authority.
        task_context_v1::presentation(
            &serde_json::to_value(self).expect("TaskContextCapsule is JSON encodable"),
        )
    }
}

fn is_lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn semantic_sha256(value: &Value) -> String {
    let canonical = canonical_json(value);
    format!("{:x}", Sha256::digest(canonical.as_bytes()))
}

fn canonical_json(value: &Value) -> String {
    match value {
        Value::Null => "null".to_string(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => serde_json::to_string(value).expect("JSON strings are encodable"),
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(canonical_json)
                .collect::<Vec<_>>()
                .join(",")
        ),
        Value::Object(values) => {
            let mut keys = values.keys().collect::<Vec<_>>();
            keys.sort();
            format!(
                "{{{}}}",
                keys.into_iter()
                    .map(|key| format!(
                        "{}:{}",
                        serde_json::to_string(key).expect("JSON object keys are encodable"),
                        canonical_json(&values[key])
                    ))
                    .collect::<Vec<_>>()
                    .join(",")
            )
        }
    }
}

impl CallContext {
    pub fn new(method: String, path: String, input: Value) -> Self {
        Self {
            request_id: uuid::Uuid::new_v4().to_string(),
            method,
            path,
            input,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct WorkerEnvelope {
    pub kind: String,
    #[serde(default)]
    pub payload: Value,
}

impl WorkerEnvelope {
    pub fn health_check() -> Self {
        Self {
            kind: WORKER_KIND_HEALTH_CHECK.to_string(),
            payload: Value::Object(Default::default()),
        }
    }

    pub fn call(context: CallContext) -> Self {
        Self {
            kind: WORKER_KIND_CALL.to_string(),
            payload: serde_json::json!({ "input": context }),
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RunAgentRequest {
    pub runtime_id: String,
    pub lease_id: String,
    #[serde(default)]
    pub fallback_from_id: Option<String>,
    #[serde(default)]
    pub lifecycle: Option<LifecycleExecutionContext>,
    #[serde(default)]
    pub task_id: Option<String>,
    #[serde(default)]
    pub goal_id: Option<String>,
    #[serde(default)]
    pub operator_override: bool,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub directory: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub agent: Option<String>,
    #[serde(default)]
    pub session_type: Option<String>,
    #[serde(default)]
    pub prompt: Option<String>,
    #[serde(default)]
    pub message: Option<String>,
    #[serde(default)]
    pub input: Option<Value>,
    #[serde(default)]
    pub parent_session_id: Option<String>,
    #[serde(default)]
    pub parent_mission_revision_sha256: Option<String>,
    #[serde(default)]
    pub delegated_input_sha256: Option<String>,
    #[serde(default)]
    pub depth: Option<usize>,
    #[serde(default)]
    pub runtime_context: Option<String>,
    #[serde(default)]
    pub planning_mode_override: Option<bool>,
    #[serde(default)]
    pub jspace_contract: Option<Value>,
    #[serde(default)]
    pub task_context_capsule: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub native_codex_execution: Option<NativeCodexExecutionBinding>,
    #[serde(default)]
    pub no_op_manual: bool,
    #[serde(default)]
    pub return_log: bool,
    #[serde(default)]
    pub maximum_parallel_runtime_workers: Option<usize>,
    #[serde(default)]
    pub worker_env: HashMap<String, String>,
}

impl RunAgentRequest {
    pub fn effective_prompt(&self) -> Option<&str> {
        self.prompt
            .as_deref()
            .or(self.message.as_deref())
            .or_else(|| self.input.as_ref().and_then(Value::as_str))
    }

    pub fn effective_prompt_sha256(&self) -> Option<String> {
        self.effective_prompt()
            .map(|value| semantic_sha256(&Value::String(value.to_string())))
    }

    pub fn validate_delegated_identity(&self) -> Result<(), String> {
        let delegated = self
            .parent_session_id
            .as_deref()
            .is_some_and(|value| !value.trim().is_empty());
        for (name, value) in [
            (
                "parent_mission_revision_sha256",
                self.parent_mission_revision_sha256.as_deref(),
            ),
            (
                "delegated_input_sha256",
                self.delegated_input_sha256.as_deref(),
            ),
        ] {
            if delegated && value.is_none_or(|value| value.trim().is_empty()) {
                return Err(format!("DELEGATED_LIFECYCLE_IDENTITY_MISSING:{name}"));
            }
            if value.is_some_and(|value| !is_lower_sha256(value)) {
                return Err(format!("DELEGATED_LIFECYCLE_IDENTITY_INVALID:{name}"));
            }
        }
        if let Some(binding) = &self.native_codex_execution {
            if !delegated {
                return Err("NATIVE_CODEX_DURABLE_PARENT_IDENTITY_MISSING".to_string());
            }
            let capsule = self
                .task_context_capsule
                .clone()
                .ok_or_else(|| "NATIVE_CODEX_DURABLE_CAPSULE_MISSING".to_string())
                .and_then(TaskContextCapsule::from_value)?;
            let task_id = self
                .task_id
                .as_deref()
                .filter(|value| !value.trim().is_empty())
                .ok_or_else(|| "NATIVE_CODEX_DURABLE_TASK_ID_MISSING".to_string())?;
            let mission_revision_sha256 = self
                .parent_mission_revision_sha256
                .as_deref()
                .ok_or_else(|| "NATIVE_CODEX_DURABLE_MISSION_REVISION_MISSING".to_string())?;
            let prompt = self
                .effective_prompt()
                .ok_or_else(|| "NATIVE_CODEX_DURABLE_TASK_DELTA_MISSING".to_string())?;
            binding.bind_authoritative_task(&capsule, task_id, mission_revision_sha256, prompt)?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeWorkerResponse {
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_state: Option<SessionState>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message_count: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_started_at_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub final_text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub session_log: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub usage: Option<Value>,
}

#[cfg(test)]
mod tests {
    use super::{ModelServiceTier, RunAgentRequest};
    use serde_json::json;

    #[test]
    fn run_agent_request_preserves_optional_retry_lineage_on_the_wire() {
        let retry: RunAgentRequest = serde_json::from_value(serde_json::json!({
            "runtime_id": "runtime-retry",
            "lease_id": "lease-retry",
            "fallback_from_id": "runtime-failed"
        }))
        .expect("retry request should decode");
        assert_eq!(retry.fallback_from_id.as_deref(), Some("runtime-failed"));

        let first: RunAgentRequest = serde_json::from_value(serde_json::json!({
            "runtime_id": "runtime-first",
            "lease_id": "lease-first"
        }))
        .expect("first request should decode without retry lineage");
        assert_eq!(first.fallback_from_id, None);
    }

    #[test]
    fn delegated_dispatch_requires_mission_revision_and_input_digest() {
        let mut request: RunAgentRequest = serde_json::from_value(json!({
            "runtime_id": "runtime-child",
            "lease_id": "lease-child",
            "session_id": "child-session",
            "parent_session_id": "019fd83e-861a-7b62-a628-0e0ad2f88a27"
        }))
        .expect("delegated request");
        assert_eq!(
            request.validate_delegated_identity().unwrap_err(),
            "DELEGATED_LIFECYCLE_IDENTITY_MISSING:parent_mission_revision_sha256"
        );
        request.parent_mission_revision_sha256 =
            Some("69edd74f732aa5bed571d652e7f91874a16881116b454218a508f413a33fcd70".to_string());
        assert_eq!(
            request.validate_delegated_identity().unwrap_err(),
            "DELEGATED_LIFECYCLE_IDENTITY_MISSING:delegated_input_sha256"
        );
    }

    #[test]
    fn top_level_dispatch_remains_compatible_without_delegated_digests() {
        let request: RunAgentRequest = serde_json::from_value(json!({
            "runtime_id": "runtime-top",
            "lease_id": "lease-top",
            "session_id": "top-session"
        }))
        .expect("top-level request");
        request
            .validate_delegated_identity()
            .expect("top-level compatibility");
    }

    #[test]
    fn model_service_tier_is_closed_and_wire_stable() {
        assert_eq!(
            "ultrafast".parse::<ModelServiceTier>(),
            Ok(ModelServiceTier::Ultrafast)
        );
        assert_eq!(
            serde_json::to_value(ModelServiceTier::Priority).expect("service tier encode"),
            json!("priority")
        );
        assert_eq!(
            "turbo".parse::<ModelServiceTier>().unwrap_err(),
            "TURA_SESSION_SERVICE_TIER_INVALID:turbo"
        );
    }
}
