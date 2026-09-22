#![forbid(unsafe_code)]

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

pub const IPC_KIND_CALL: &str = "call";
pub const IPC_KIND_HEALTH_CHECK: &str = "health_check";
pub const METHOD_HEALTH_CHECK: &str = "health_check";
pub const METHOD_ENQUEUE_TURN: &str = "execution.enqueue_turn";
pub const METHOD_REGISTER_CHILD_SESSION: &str = "execution.register_child_session";
pub const METHOD_COMMANDER_TASK_PACKET_CAPABILITIES: &str =
    "execution.commander.task_packet_capabilities";
pub const METHOD_COMPILE_COMMANDER_TASK_PACKET: &str = "execution.commander.compile_task_packet";
pub const METHOD_DISPATCH_COMMANDER_TASK_PACKET: &str = "execution.commander.dispatch_task_packet";
pub const COMMANDER_TASK_PACKET_SCHEMA_VERSION: &str = "tura_commander_task_packet_v1";
pub const COMMANDER_DISPATCH_PROTOCOL_VERSION: &str = "tura_commander_dispatch_protocol_v1";
pub const METHOD_ACKNOWLEDGE_CHILD_CALLBACK: &str = "execution.acknowledge_child_callback";
pub const METHOD_READ_CONTROL_DECK_CONVERGENCE: &str = "execution.control_deck.convergence";
pub const METHOD_READ_TASK_READY_SET: &str = "execution.read_task_ready_set";
pub const MAX_CONTROL_DECK_COMMANDER_SESSION_IDS: usize = 128;
pub const MAX_TASK_READY_SET_TASK_IDS: usize = 128;
pub const METHOD_LIST_COMMANDS: &str = "registry.commands.list";
pub const METHOD_EXECUTE_COMMAND: &str = "registry.commands.execute";
pub const METHOD_LIST_TOOLS: &str = "registry.tools.list";
pub const METHOD_GET_TOOL: &str = "registry.tools.get";
pub const METHOD_PATCH_TOOL: &str = "registry.tools.patch";
pub const METHOD_GET_TOOL_CONFIG: &str = "registry.tools.config.get";
pub const METHOD_PATCH_TOOL_CONFIG: &str = "registry.tools.config.patch";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RouterEndpoint {
    pub addr: String,
    pub version: String,
    #[serde(default)]
    pub binary_sha256: Option<String>,
    pub pid: Option<u32>,
    pub process_start_time: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct IpcRequest {
    pub request_id: String,
    pub kind: String,
    #[serde(default)]
    pub method: String,
    #[serde(default)]
    pub payload: Value,
    #[serde(default)]
    pub deadline_ms: Option<u64>,
}

impl IpcRequest {
    pub fn call(request_id: impl Into<String>, method: impl Into<String>, payload: Value) -> Self {
        Self {
            request_id: request_id.into(),
            kind: IPC_KIND_CALL.to_string(),
            method: method.into(),
            payload,
            deadline_ms: None,
        }
    }

    pub fn health_check(request_id: impl Into<String>, deadline_ms: u64) -> Self {
        Self {
            request_id: request_id.into(),
            kind: IPC_KIND_HEALTH_CHECK.to_string(),
            method: METHOD_HEALTH_CHECK.to_string(),
            payload: Value::Object(Default::default()),
            deadline_ms: Some(deadline_ms),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct IpcResponse {
    pub request_id: String,
    pub ok: bool,
    #[serde(default)]
    pub payload: Value,
    #[serde(default)]
    pub error: Option<String>,
}

impl IpcResponse {
    pub fn ok(request_id: impl Into<String>, payload: Value) -> Self {
        Self {
            request_id: request_id.into(),
            ok: true,
            payload,
            error: None,
        }
    }

    pub fn error(request_id: impl Into<String>, error: impl Into<String>) -> Self {
        Self {
            request_id: request_id.into(),
            ok: false,
            payload: Value::Null,
            error: Some(error.into()),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EnqueueTurnRequest {
    pub runtime_id: String,
    pub session_id: String,
    pub payload: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReadControlDeckConvergenceRequest {
    pub commander_session_ids: Vec<String>,
}

impl ReadControlDeckConvergenceRequest {
    pub fn validate(&self) -> Result<(), String> {
        if self.commander_session_ids.len() > MAX_CONTROL_DECK_COMMANDER_SESSION_IDS {
            return Err(format!(
                "CONTROL_DECK_COMMANDER_SESSION_LIMIT_EXCEEDED:{}",
                MAX_CONTROL_DECK_COMMANDER_SESSION_IDS
            ));
        }
        if self
            .commander_session_ids
            .iter()
            .any(|session_id| session_id.trim().is_empty())
        {
            return Err("CONTROL_DECK_COMMANDER_SESSION_ID_MISSING".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ControlDeckConvergenceAvailability {
    Present,
    TypedAbsence,
    Unavailable,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ControlDeckConvergenceEntry {
    pub commander_session_id: String,
    pub availability: ControlDeckConvergenceAvailability,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub projection: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub blocker_code: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ReadControlDeckConvergenceResponse {
    pub schema_version: String,
    pub entries: Vec<ControlDeckConvergenceEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReadTaskReadySetRequest {
    pub parent_session_id: String,
    pub authority_mission_revision_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expected_parent_task_plan_sha256: Option<String>,
    pub task_ids: Vec<String>,
}

impl ReadTaskReadySetRequest {
    pub fn validate(&self) -> Result<(), String> {
        if self.parent_session_id.trim().is_empty() {
            return Err("TASK_READY_SET_PARENT_SESSION_ID_MISSING".to_string());
        }
        if !is_lower_hex_sha256(&self.authority_mission_revision_sha256) {
            return Err("TASK_READY_SET_AUTHORITY_REVISION_INVALID".to_string());
        }
        if self
            .expected_parent_task_plan_sha256
            .as_deref()
            .is_some_and(|value| !is_lower_hex_sha256(value))
        {
            return Err("TASK_READY_SET_PARENT_PLAN_IDENTITY_INVALID".to_string());
        }
        if self.task_ids.is_empty() || self.task_ids.len() > MAX_TASK_READY_SET_TASK_IDS {
            return Err(format!(
                "TASK_READY_SET_TASK_ID_LIMIT_INVALID:{}",
                MAX_TASK_READY_SET_TASK_IDS
            ));
        }
        if self.task_ids.iter().any(|value| value.trim().is_empty()) {
            return Err("TASK_READY_SET_TASK_ID_MISSING".to_string());
        }
        let mut canonical = self.task_ids.clone();
        canonical.sort();
        canonical.dedup();
        if canonical.len() != self.task_ids.len() {
            return Err("TASK_READY_SET_TASK_ID_DUPLICATE".to_string());
        }
        if canonical != self.task_ids {
            return Err("TASK_READY_SET_TASK_IDS_NOT_CANONICAL".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TaskReadySetWireState {
    Ready,
    BlockedState,
    BlockedDependency,
    BlockedScope,
    BlockedLease,
    BlockedCapacity,
    TypedUnknown,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TaskReadySetEntry {
    pub task_id: String,
    pub state: TaskReadySetWireState,
    pub reason_codes: Vec<String>,
    pub blocking_task_ids: Vec<String>,
    pub dependency_task_ids: Vec<String>,
    pub active_runtime_count: usize,
    pub active_lease_ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub semantic_dispatch_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_scheduling_contract_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scope_claim_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub maximum_parallel_runtime_workers: Option<usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReadTaskReadySetResponse {
    pub schema_version: String,
    pub parent_session_id: String,
    pub parent_task_plan_sha256: String,
    pub state_head: String,
    pub authority_effect: String,
    pub entries: Vec<TaskReadySetEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RegisterChildSessionRequest {
    pub parent_session_id: String,
    pub parent_mission_revision_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub commander_thread_id: Option<String>,
    pub child_session_id: String,
    pub child_runtime_id: String,
    pub child_transaction_id: String,
    pub child_lease_id: String,
    pub callback_request_id: String,
    pub effect_id: String,
    pub callback_delivery_route: CallbackDeliveryRoute,
    pub delegated_input_sha256: String,
    pub session_directory: String,
    pub session_name: String,
    pub created_at_ms: i64,
    pub execution_payload: Value,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CallbackDeliveryRoute {
    TrustedTuraDirectThreadWriter,
}

impl RegisterChildSessionRequest {
    pub fn canonical_effect_id(&self) -> String {
        format!("{}.message", self.child_runtime_id)
    }

    pub fn validate(&self) -> Result<(), String> {
        for (name, value) in [
            ("parent_session_id", self.parent_session_id.as_str()),
            ("child_session_id", self.child_session_id.as_str()),
            ("child_runtime_id", self.child_runtime_id.as_str()),
            ("child_transaction_id", self.child_transaction_id.as_str()),
            ("child_lease_id", self.child_lease_id.as_str()),
            ("callback_request_id", self.callback_request_id.as_str()),
            ("effect_id", self.effect_id.as_str()),
            ("session_directory", self.session_directory.as_str()),
            ("session_name", self.session_name.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(format!("CHILD_ADMISSION_IDENTITY_MISSING:{name}"));
            }
        }
        match self.commander_thread_id.as_deref() {
            None => {
                return Err("CHILD_ADMISSION_IDENTITY_MISSING:commander_thread_id".to_string());
            }
            Some(value) if value.trim().is_empty() => {
                return Err("CHILD_ADMISSION_IDENTITY_INVALID:commander_thread_id".to_string());
            }
            Some(_) => {}
        }
        for (name, value) in [
            (
                "parent_mission_revision_sha256",
                self.parent_mission_revision_sha256.as_str(),
            ),
            (
                "delegated_input_sha256",
                self.delegated_input_sha256.as_str(),
            ),
        ] {
            if value.len() != 64
                || !value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            {
                return Err(format!("CHILD_ADMISSION_IDENTITY_INVALID:{name}"));
            }
        }
        if self.parent_session_id == self.child_session_id {
            return Err("CHILD_ADMISSION_IDENTITY_CONFLICT:parent_equals_child".to_string());
        }
        if self.callback_request_id != self.child_transaction_id {
            return Err(
                "CHILD_ADMISSION_CALLBACK_IDENTITY_CONFLICT:callback_request_id".to_string(),
            );
        }
        let canonical_effect_id = self.canonical_effect_id();
        if self.effect_id != canonical_effect_id {
            return Err(format!(
                "CHILD_ADMISSION_EFFECT_IDENTITY_CONFLICT:expected={canonical_effect_id},actual={}",
                self.effect_id
            ));
        }
        if self.created_at_ms <= 0 {
            return Err("CHILD_ADMISSION_IDENTITY_INVALID:created_at_ms".to_string());
        }
        if !self.execution_payload.is_object() {
            return Err("CHILD_ADMISSION_PAYLOAD_INVALID:execution_payload".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RegisterChildSessionOutcome {
    Admitted,
    AlreadyAdmitted,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RegisterChildSessionResponse {
    pub outcome: RegisterChildSessionOutcome,
    pub parent_session_id: String,
    pub child_session_id: String,
    pub child_runtime_id: String,
    pub child_transaction_id: String,
    pub callback_request_id: String,
    pub effect_id: String,
    pub callback_delivery_route: CallbackDeliveryRoute,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CommanderTaskPacketV1 {
    pub schema_version: String,
    pub parent_session_id: String,
    pub parent_mission_revision_sha256: String,
    pub commander_thread_id: String,
    pub task_id: String,
    pub session_directory: String,
    pub session_name: String,
    pub created_at_ms: i64,
    pub prompt: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub service_tier: Option<runtime_contract::ModelServiceTier>,
    pub maximum_parallel_runtime_workers: usize,
    pub task_context_capsule: Value,
    pub jspace_contract: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub native_codex_execution: Option<runtime_contract::NativeCodexExecutionBinding>,
}

impl CommanderTaskPacketV1 {
    pub fn validate_shape(&self) -> Result<(), String> {
        if self.schema_version != COMMANDER_TASK_PACKET_SCHEMA_VERSION {
            return Err(format!(
                "TASK_PACKET_SCHEMA_VERSION_UNSUPPORTED:expected={COMMANDER_TASK_PACKET_SCHEMA_VERSION},actual={}",
                self.schema_version
            ));
        }
        for (name, value) in [
            ("parent_session_id", self.parent_session_id.as_str()),
            ("commander_thread_id", self.commander_thread_id.as_str()),
            ("task_id", self.task_id.as_str()),
            ("session_directory", self.session_directory.as_str()),
            ("session_name", self.session_name.as_str()),
            ("prompt", self.prompt.as_str()),
        ] {
            if value.trim().is_empty() || value.trim() != value {
                return Err(format!("TASK_PACKET_FIELD_INVALID:{name}"));
            }
        }
        if self.parent_mission_revision_sha256.len() != 64
            || !self
                .parent_mission_revision_sha256
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err("TASK_PACKET_MISSION_REVISION_INVALID".to_string());
        }
        if self.created_at_ms <= 0 {
            return Err("TASK_PACKET_CREATED_AT_INVALID".to_string());
        }
        if self.maximum_parallel_runtime_workers == 0 {
            return Err("TASK_PACKET_PARALLEL_LIMIT_INVALID".to_string());
        }
        for (name, value) in [
            ("model", self.model.as_deref()),
            ("agent", self.agent.as_deref()),
            ("session_type", self.session_type.as_deref()),
        ] {
            if value.is_some_and(|value| value.trim().is_empty() || value.trim() != value) {
                return Err(format!("TASK_PACKET_FIELD_INVALID:{name}"));
            }
        }
        if !self.task_context_capsule.is_object() {
            return Err("TASK_PACKET_CAPSULE_INVALID".to_string());
        }
        if !self.jspace_contract.is_object() {
            return Err("TASK_PACKET_JSPACE_INVALID".to_string());
        }
        if let Some(binding) = &self.native_codex_execution {
            binding.validate()?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CommanderTaskPacketCapabilities {
    pub schema_version: String,
    pub protocol_version: String,
    pub task_packet_schema_versions: Vec<String>,
    pub callback_delivery_route: CallbackDeliveryRoute,
    pub compile_only: bool,
    pub idempotent_replay: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CommanderMutationCounts {
    pub parent_claim: u64,
    pub child: u64,
    pub runtime: u64,
    pub callback: u64,
}

impl CommanderMutationCounts {
    pub fn zero() -> Self {
        Self {
            parent_claim: 0,
            child: 0,
            runtime: 0,
            callback: 0,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CommanderTaskPacketCompileResult {
    pub schema_version: String,
    pub protocol_version: String,
    pub task_packet_schema_version: String,
    pub compile_identity_sha256: String,
    pub semantic_dispatch_key: String,
    pub parent_task_plan_sha256: String,
    pub task_scheduling_contract_sha256: String,
    pub scope_claim_sha256: String,
    pub task_context_capsule_semantic_sha256: String,
    pub jspace_authorization_semantic_sha256: String,
    pub delegated_input_sha256: String,
    pub parent_session_id: String,
    pub parent_mission_revision_sha256: String,
    pub commander_thread_id: String,
    pub task_id: String,
    pub child_session_id: String,
    pub child_runtime_id: String,
    pub child_transaction_id: String,
    pub child_lease_id: String,
    pub callback_request_id: String,
    pub effect_id: String,
    pub callback_delivery_route: CallbackDeliveryRoute,
    pub authority_effect: String,
    pub mutation_counts: CommanderMutationCounts,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CommanderTaskPacketDispatchResponse {
    pub schema_version: String,
    pub compilation: CommanderTaskPacketCompileResult,
    pub admission: RegisterChildSessionResponse,
    pub duplicate_effect_count: u64,
    pub commander_mission_verification_required: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum AcknowledgeChildCallbackEffectIdentity {
    Exact {
        effect_id: String,
    },
    ProvenZeroEffect {
        classification: String,
        evidence_sha256: String,
    },
    UnsettledEffect {
        classification: String,
        evidence_sha256: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AcknowledgeChildCallbackRequest {
    pub parent_session_id: String,
    pub parent_mission_revision_sha256: String,
    pub commander_thread_id: String,
    pub child_session_id: String,
    pub child_runtime_id: String,
    pub child_lease_id: String,
    pub transaction_id: String,
    pub event_id: String,
    pub callback_payload_sha256: String,
    pub effect_identity: AcknowledgeChildCallbackEffectIdentity,
}

impl AcknowledgeChildCallbackRequest {
    pub fn validate(&self) -> Result<(), String> {
        for (name, value) in [
            ("parent_session_id", self.parent_session_id.as_str()),
            ("commander_thread_id", self.commander_thread_id.as_str()),
            ("child_session_id", self.child_session_id.as_str()),
            ("child_runtime_id", self.child_runtime_id.as_str()),
            ("child_lease_id", self.child_lease_id.as_str()),
            ("transaction_id", self.transaction_id.as_str()),
            ("event_id", self.event_id.as_str()),
        ] {
            if value.trim().is_empty() {
                return Err(format!("CHILD_CALLBACK_ACK_IDENTITY_MISSING:{name}"));
            }
        }
        for (name, value) in [
            (
                "parent_mission_revision_sha256",
                self.parent_mission_revision_sha256.as_str(),
            ),
            (
                "callback_payload_sha256",
                self.callback_payload_sha256.as_str(),
            ),
        ] {
            if !is_lower_hex_sha256(value) {
                return Err(format!("CHILD_CALLBACK_ACK_IDENTITY_INVALID:{name}"));
            }
        }
        if self.parent_session_id == self.child_session_id {
            return Err("CHILD_CALLBACK_ACK_IDENTITY_CONFLICT:parent_equals_child".to_string());
        }
        match &self.effect_identity {
            AcknowledgeChildCallbackEffectIdentity::Exact { effect_id } => {
                if effect_id.trim().is_empty() {
                    return Err("CHILD_CALLBACK_ACK_IDENTITY_MISSING:effect_id".to_string());
                }
            }
            AcknowledgeChildCallbackEffectIdentity::ProvenZeroEffect {
                classification,
                evidence_sha256,
            }
            | AcknowledgeChildCallbackEffectIdentity::UnsettledEffect {
                classification,
                evidence_sha256,
            } => {
                if classification.trim().is_empty() {
                    return Err(
                        "CHILD_CALLBACK_ACK_IDENTITY_MISSING:effect_classification".to_string()
                    );
                }
                if !is_lower_hex_sha256(evidence_sha256) {
                    return Err(
                        "CHILD_CALLBACK_ACK_IDENTITY_INVALID:effect_evidence_sha256".to_string()
                    );
                }
            }
        }
        Ok(())
    }
}

fn is_lower_hex_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum AcknowledgeChildCallbackOutcome {
    Acknowledged,
    AlreadyAcknowledged,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AcknowledgeChildCallbackResponse {
    pub outcome: AcknowledgeChildCallbackOutcome,
    pub parent_session_id: String,
    pub parent_mission_revision_sha256: String,
    pub commander_thread_id: String,
    pub child_session_id: String,
    pub child_runtime_id: String,
    pub child_lease_id: String,
    pub transaction_id: String,
    pub event_id: String,
    pub callback_payload_sha256: String,
    pub effect_identity: AcknowledgeChildCallbackEffectIdentity,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CancelRuntimeRequest {
    pub session_id: String,
    pub runtime_id: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProbeSessionsRequest {
    #[serde(default)]
    pub session_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ListCommandsRequest {
    pub directory: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CommandSpec {
    pub name: String,
    pub description: String,
    pub agent: Option<String>,
    pub model: Option<String>,
    pub source: String,
    pub template: Option<String>,
    pub subtask: bool,
    pub hints: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ListCommandsResponse {
    pub commands: Vec<CommandSpec>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ExecuteCommandRequest {
    pub directory: Option<String>,
    pub command: String,
    pub args: Option<Vec<String>>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ExecuteCommandResponse {
    pub output: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ToolRegistryRequest {
    pub repo_root: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ToolRequest {
    pub repo_root: String,
    pub tool_id: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ConfigurableEntry {
    pub key: String,
    #[serde(default)]
    pub label: String,
    pub description: String,
    #[serde(rename = "type")]
    pub value_type: String,
    pub default: Value,
    #[serde(default, rename = "enum")]
    pub enum_values: Vec<String>,
    #[serde(default)]
    pub required: bool,
    #[serde(default = "default_config_scope")]
    pub scope: String,
}

fn default_config_scope() -> String {
    "workspace".to_string()
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ToolState {
    Discovered,
    Configured,
    Enabled,
    Disabled,
    Unavailable,
    Running,
    Succeeded,
    Failed,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ToolView {
    pub id: String,
    pub name: String,
    pub description: String,
    pub core: bool,
    pub category: String,
    pub execution: String,
    pub enabled: bool,
    pub aliases: Vec<String>,
    pub supports_macro_command: bool,
    pub mutating: bool,
    pub network: bool,
    pub configurable: Vec<ConfigurableEntry>,
    pub state: ToolState,
    pub binary: Option<String>,
    pub binary_path: Option<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ToolPatch {
    pub enabled: Option<bool>,
    pub aliases: Option<Vec<String>>,
    pub core: Option<bool>,
    pub execution: Option<String>,
    pub binary: Option<String>,
    pub mutating: Option<bool>,
    pub network: Option<bool>,
    pub policy: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PatchToolRequest {
    pub repo_root: String,
    pub tool_id: String,
    pub patch: ToolPatch,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ToolConfigResponse {
    pub id: String,
    pub configurable: Vec<ConfigurableEntry>,
    pub values: BTreeMap<String, Value>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PatchToolConfigRequest {
    pub repo_root: String,
    pub tool_id: String,
    pub values: BTreeMap<String, Value>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ListToolsResponse {
    pub tools: Vec<ToolView>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct GetToolResponse {
    pub tool: Option<ToolView>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct GetToolConfigResponse {
    pub config: Option<ToolConfigResponse>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn child_request() -> RegisterChildSessionRequest {
        RegisterChildSessionRequest {
            parent_session_id: "parent-1".to_string(),
            parent_mission_revision_sha256: "a".repeat(64),
            commander_thread_id: Some("commander-thread-1".to_string()),
            child_session_id: "child-1".to_string(),
            child_runtime_id: "runtime-1".to_string(),
            child_transaction_id: "callback-1".to_string(),
            child_lease_id: "lease-1".to_string(),
            callback_request_id: "callback-1".to_string(),
            effect_id: "runtime-1.message".to_string(),
            callback_delivery_route: CallbackDeliveryRoute::TrustedTuraDirectThreadWriter,
            delegated_input_sha256: "b".repeat(64),
            session_directory: "/tmp/child-1".to_string(),
            session_name: "delegated child".to_string(),
            created_at_ms: 1_788_000_000_000,
            execution_payload: json!({"prompt": "perform delegated work"}),
        }
    }

    fn task_packet() -> CommanderTaskPacketV1 {
        CommanderTaskPacketV1 {
            schema_version: COMMANDER_TASK_PACKET_SCHEMA_VERSION.to_string(),
            parent_session_id: "parent-1".to_string(),
            parent_mission_revision_sha256: "a".repeat(64),
            commander_thread_id: "thread-1".to_string(),
            task_id: "task-1".to_string(),
            session_directory: "/tmp/workspace/child".to_string(),
            session_name: "delegated child".to_string(),
            created_at_ms: 1_788_000_000_000,
            prompt: "perform delegated work".to_string(),
            model: Some("official_codex_app_server/gpt-5.6-sol".to_string()),
            agent: Some("balanced".to_string()),
            session_type: None,
            service_tier: None,
            maximum_parallel_runtime_workers: 4,
            task_context_capsule: json!({"schema_version": "task_context_capsule_v1"}),
            jspace_contract: json!({"schema_version": "jspace_contract_v2"}),
            native_codex_execution: None,
        }
    }

    fn callback_ack_request() -> AcknowledgeChildCallbackRequest {
        AcknowledgeChildCallbackRequest {
            parent_session_id: "parent-1".to_string(),
            parent_mission_revision_sha256: "a".repeat(64),
            commander_thread_id: "commander-thread-1".to_string(),
            child_session_id: "child-1".to_string(),
            child_runtime_id: "runtime-1".to_string(),
            child_lease_id: "lease-1".to_string(),
            transaction_id: "callback-1".to_string(),
            event_id: "event-1".to_string(),
            callback_payload_sha256: "b".repeat(64),
            effect_identity: AcknowledgeChildCallbackEffectIdentity::Exact {
                effect_id: "runtime-1.message".to_string(),
            },
        }
    }

    #[test]
    fn control_deck_request_is_bounded_and_preserves_typed_absence() {
        ReadControlDeckConvergenceRequest {
            commander_session_ids: vec!["commander-1".to_string()],
        }
        .validate()
        .expect("bounded request");
        let oversized = ReadControlDeckConvergenceRequest {
            commander_session_ids: (0..=MAX_CONTROL_DECK_COMMANDER_SESSION_IDS)
                .map(|index| format!("commander-{index}"))
                .collect(),
        };
        assert_eq!(
            oversized.validate().unwrap_err(),
            format!(
                "CONTROL_DECK_COMMANDER_SESSION_LIMIT_EXCEEDED:{}",
                MAX_CONTROL_DECK_COMMANDER_SESSION_IDS
            )
        );
        let response = ReadControlDeckConvergenceResponse {
            schema_version: "tura_control_deck_convergence_response_v1".to_string(),
            entries: vec![ControlDeckConvergenceEntry {
                commander_session_id: "commander-absent".to_string(),
                availability: ControlDeckConvergenceAvailability::TypedAbsence,
                projection: None,
                blocker_code: Some("LIFECYCLE_STORE_ABSENT".to_string()),
            }],
        };
        let value = serde_json::to_value(response).expect("serialize response");
        assert_eq!(value["entries"][0]["availability"], "typed_absence");
        assert!(value["entries"][0].get("projection").is_none());
    }

    #[test]
    fn task_ready_set_contract_is_bounded_and_digest_only() {
        let request = ReadTaskReadySetRequest {
            parent_session_id: "parent-1".to_string(),
            authority_mission_revision_sha256: "a".repeat(64),
            expected_parent_task_plan_sha256: Some("b".repeat(64)),
            task_ids: vec!["task-a".to_string(), "task-b".to_string()],
        };
        request.validate().expect("bounded ready-set request");

        let mut duplicate = request.clone();
        duplicate.task_ids = vec!["task-a".to_string(), "task-a".to_string()];
        assert_eq!(
            duplicate.validate().unwrap_err(),
            "TASK_READY_SET_TASK_ID_DUPLICATE"
        );

        let response = ReadTaskReadySetResponse {
            schema_version: "tura_task_ready_set_response_v1".to_string(),
            parent_session_id: request.parent_session_id,
            parent_task_plan_sha256: "b".repeat(64),
            state_head: "c".repeat(64),
            authority_effect: "none".to_string(),
            entries: vec![TaskReadySetEntry {
                task_id: "task-a".to_string(),
                state: TaskReadySetWireState::TypedUnknown,
                reason_codes: vec!["TASK_READY_SET_SCHEDULING_CONTRACT_MISSING".to_string()],
                blocking_task_ids: vec![],
                dependency_task_ids: vec![],
                active_runtime_count: 0,
                active_lease_ids: vec![],
                semantic_dispatch_key: None,
                task_scheduling_contract_sha256: None,
                scope_claim_sha256: None,
                maximum_parallel_runtime_workers: None,
            }],
        };
        let value = serde_json::to_value(response).expect("ready-set response");
        assert_eq!(value["authority_effect"], "none");
        assert!(value["entries"][0].get("scope_claim_sha256").is_none());
    }

    #[test]
    fn child_admission_contract_binds_all_required_identities() {
        let request = child_request();
        request.validate().expect("valid child admission");
        let encoded = serde_json::to_value(&request).expect("serialize request");
        let decoded: RegisterChildSessionRequest =
            serde_json::from_value(encoded).expect("deserialize request");
        assert_eq!(decoded, request);
        assert_eq!(
            serde_json::to_value(&request)
                .expect("serialize request")
                .get("callback_delivery_route"),
            Some(&json!("trusted_tura_direct_thread_writer"))
        );
        let response = RegisterChildSessionResponse {
            outcome: RegisterChildSessionOutcome::Admitted,
            parent_session_id: request.parent_session_id.clone(),
            child_session_id: request.child_session_id.clone(),
            child_runtime_id: request.child_runtime_id.clone(),
            child_transaction_id: request.child_transaction_id.clone(),
            callback_request_id: request.callback_request_id.clone(),
            effect_id: request.effect_id.clone(),
            callback_delivery_route: request.callback_delivery_route,
        };
        let response_value = serde_json::to_value(&response).expect("serialize response");
        assert_eq!(
            response_value["callback_delivery_route"],
            "trusted_tura_direct_thread_writer"
        );
        assert_eq!(
            serde_json::from_value::<RegisterChildSessionResponse>(response_value)
                .expect("deserialize response"),
            response
        );
    }

    #[test]
    fn commander_task_packet_is_strict_versioned_and_contains_no_wire_identities() {
        let packet = task_packet();
        packet.validate_shape().expect("valid packet shape");
        let encoded = serde_json::to_value(&packet).expect("serialize packet");
        for forbidden in [
            "child_session_id",
            "child_runtime_id",
            "child_transaction_id",
            "child_lease_id",
            "callback_request_id",
            "effect_id",
            "callback_delivery_route",
            "semantic_dispatch_key",
        ] {
            assert!(encoded.get(forbidden).is_none(), "{forbidden}");
        }
        let mut unknown = encoded.clone();
        unknown["effect_id"] = json!("user-authored-effect");
        assert!(serde_json::from_value::<CommanderTaskPacketV1>(unknown).is_err());

        let mut wrong_version = packet;
        wrong_version.schema_version = "tura_commander_task_packet_v0".to_string();
        assert!(
            wrong_version
                .validate_shape()
                .unwrap_err()
                .starts_with("TASK_PACKET_SCHEMA_VERSION_UNSUPPORTED")
        );
    }

    #[test]
    fn child_admission_contract_rejects_missing_or_unknown_delivery_route() {
        let mut missing = serde_json::to_value(child_request()).expect("serialize request");
        missing
            .as_object_mut()
            .expect("request object")
            .remove("callback_delivery_route");
        assert!(serde_json::from_value::<RegisterChildSessionRequest>(missing).is_err());

        let mut unknown = serde_json::to_value(child_request()).expect("serialize request");
        unknown["callback_delivery_route"] = json!("codex_owned_adapter");
        assert!(serde_json::from_value::<RegisterChildSessionRequest>(unknown).is_err());
    }

    #[test]
    fn child_admission_contract_rejects_missing_and_changed_callback_identity() {
        let mut missing_commander_thread = child_request();
        missing_commander_thread.commander_thread_id = None;
        assert_eq!(
            missing_commander_thread.validate().unwrap_err(),
            "CHILD_ADMISSION_IDENTITY_MISSING:commander_thread_id"
        );

        let mut blank_commander_thread = child_request();
        blank_commander_thread.commander_thread_id = Some("   ".to_string());
        assert_eq!(
            blank_commander_thread.validate().unwrap_err(),
            "CHILD_ADMISSION_IDENTITY_INVALID:commander_thread_id"
        );

        let mut missing_parent = child_request();
        missing_parent.parent_session_id.clear();
        assert_eq!(
            missing_parent.validate().unwrap_err(),
            "CHILD_ADMISSION_IDENTITY_MISSING:parent_session_id"
        );

        let mut missing_child = child_request();
        missing_child.child_session_id.clear();
        assert_eq!(
            missing_child.validate().unwrap_err(),
            "CHILD_ADMISSION_IDENTITY_MISSING:child_session_id"
        );

        let mut changed_callback = child_request();
        changed_callback.callback_request_id = "callback-2".to_string();
        assert_eq!(
            changed_callback.validate().unwrap_err(),
            "CHILD_ADMISSION_CALLBACK_IDENTITY_CONFLICT:callback_request_id"
        );

        let mut missing_effect = child_request();
        missing_effect.effect_id.clear();
        assert_eq!(
            missing_effect.validate().unwrap_err(),
            "CHILD_ADMISSION_IDENTITY_MISSING:effect_id"
        );

        let mut changed_effect = child_request();
        changed_effect.effect_id = "foreign.message".to_string();
        assert_eq!(
            changed_effect.validate().unwrap_err(),
            "CHILD_ADMISSION_EFFECT_IDENTITY_CONFLICT:expected=runtime-1.message,actual=foreign.message"
        );

        let mut uppercase_revision = child_request();
        uppercase_revision.parent_mission_revision_sha256 = "A".repeat(64);
        assert_eq!(
            uppercase_revision.validate().unwrap_err(),
            "CHILD_ADMISSION_IDENTITY_INVALID:parent_mission_revision_sha256"
        );
    }

    #[test]
    fn child_callback_ack_contract_binds_exact_and_zero_effect_identities() {
        let exact = callback_ack_request();
        exact.validate().expect("valid exact callback ack");
        let decoded: AcknowledgeChildCallbackRequest = serde_json::from_value(
            serde_json::to_value(&exact).expect("serialize exact callback ack"),
        )
        .expect("deserialize exact callback ack");
        assert_eq!(decoded, exact);

        let mut zero = callback_ack_request();
        zero.effect_identity = AcknowledgeChildCallbackEffectIdentity::ProvenZeroEffect {
            classification: "pre_provider_zero_effect".to_string(),
            evidence_sha256: "c".repeat(64),
        };
        zero.validate().expect("valid zero-effect callback ack");
    }

    #[test]
    fn child_callback_ack_contract_rejects_missing_or_malformed_identity() {
        let mut missing_thread = callback_ack_request();
        missing_thread.commander_thread_id = "   ".to_string();
        assert_eq!(
            missing_thread.validate().unwrap_err(),
            "CHILD_CALLBACK_ACK_IDENTITY_MISSING:commander_thread_id"
        );

        let mut bad_payload_hash = callback_ack_request();
        bad_payload_hash.callback_payload_sha256 = "B".repeat(64);
        assert_eq!(
            bad_payload_hash.validate().unwrap_err(),
            "CHILD_CALLBACK_ACK_IDENTITY_INVALID:callback_payload_sha256"
        );

        let mut missing_effect = callback_ack_request();
        missing_effect.effect_identity = AcknowledgeChildCallbackEffectIdentity::Exact {
            effect_id: String::new(),
        };
        assert_eq!(
            missing_effect.validate().unwrap_err(),
            "CHILD_CALLBACK_ACK_IDENTITY_MISSING:effect_id"
        );
    }
}
