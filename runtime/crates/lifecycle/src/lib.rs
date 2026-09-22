#![deny(clippy::unwrap_used)]
#![deny(unsafe_code)]

mod runtime;
mod session;
mod session_management;
mod session_projection;

pub use runtime::{
    AgentId, ContextTokenStats, DEFAULT_CONTEXT_TOKEN_LIMIT, ProviderConfig, RuntimeAggregate,
    RuntimeCallResultStatus, RuntimeCommand, RuntimeError, RuntimeEvent, RuntimeId,
    RuntimeProjection, RuntimeProviderConfig, RuntimeQuery, RuntimeState, RuntimeTransitionError,
    ToolCallRecord, ToolChoice, UsageReport,
};
pub use session::{
    ACKNOWLEDGED_CHILD_CALLBACK_IDENTITY_SCHEMA_VERSION, AcknowledgedChildCallbackIdentityV1,
    PlanStatus, PollInterval, SessionAggregate, SessionCommand, SessionEvent, SessionId,
    SessionProjection, SessionQuery, SessionState, SessionTaskPatch, SessionTaskPlanPatch,
    SessionTransitionError, StartCondition, TASK_DISPATCH_CLAIM_SCHEMA_VERSION,
    TASK_SCHEDULING_CONTRACT_SCHEMA_VERSION, TaskDispatchClaimV1, TaskLeaseReadinessEvidence,
    TaskPlan, TaskReadySetDecision, TaskReadySetEvidence, TaskReadySetState,
    TaskSchedulingContractV1, TaskStep, canonical_value_sha256, classify_task_ready_set,
    task_plan_ready_set_sha256,
};
pub use session_management::{
    AgentName, DeliverableDescription, DeliverablePath, FileInput, IntoSessionTaskType,
    SESSION_CONTEXT_TOKEN_LIMIT, SessionCapabilities, SessionInput, SessionLog,
    SessionLogCompactionPoint, SessionLogEntry, SessionLogRetention, SessionManagement,
    SessionManagementDelta, SessionName, SessionTaskType, StepContext, StepToolJson, TaskStatus,
    UserGoal, UserInputText, UtcDateTimeMs,
};
