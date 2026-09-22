//! Router-owned execution supervision.
//!
//! This module owns runtime worker lifecycle decisions. Gateway may enqueue or
//! cancel turns, but must not spawn runtime workers directly.

use anyhow::{Result, anyhow};
use lifecycle::{
    PlanStatus, RuntimeAggregate, RuntimeId, RuntimeState, SessionCommand, SessionState,
    TaskDispatchClaimV1, TaskLeaseReadinessEvidence, TaskPlan, TaskReadySetDecision,
    TaskReadySetEvidence, TaskReadySetState, TaskSchedulingContractV1, classify_task_ready_set,
};
use parking_lot::Mutex;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    path::Path,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::{Notify, RwLock};
use tura_llm_rust::official_codex_app_server::load_terminal_commander_convergence_ledger;

#[cfg(unix)]
use crate::services::direct_thread_writer::DirectThreadWriterClient;
use crate::services::runtime_workers::{MAX_QUEUED_RUNTIME_TURNS, runtime_worker_limit};
use crate::{AppState, dispatch_run_agent_with_runtime_slot};
use router_contract::{
    AcknowledgeChildCallbackEffectIdentity, AcknowledgeChildCallbackOutcome,
    AcknowledgeChildCallbackRequest, AcknowledgeChildCallbackResponse,
    COMMANDER_DISPATCH_PROTOCOL_VERSION, COMMANDER_TASK_PACKET_SCHEMA_VERSION,
    CallbackDeliveryRoute as WireCallbackDeliveryRoute, CancelRuntimeRequest,
    CommanderMutationCounts, CommanderTaskPacketCapabilities, CommanderTaskPacketCompileResult,
    CommanderTaskPacketDispatchResponse, CommanderTaskPacketV1, ControlDeckConvergenceAvailability,
    ControlDeckConvergenceEntry, EnqueueTurnRequest, ProbeSessionsRequest,
    ReadControlDeckConvergenceRequest, ReadControlDeckConvergenceResponse, ReadTaskReadySetRequest,
    ReadTaskReadySetResponse, RegisterChildSessionOutcome, RegisterChildSessionRequest,
    RegisterChildSessionResponse, TaskReadySetEntry, TaskReadySetWireState,
};
use runtime_contract::{
    CommanderContinuationBinding, CommanderConvergenceProof, LifecycleExecutionContext,
    RunAgentRequest, TaskContextCapsule, maximum_parallel_runtime_workers,
};
use session_lifecycle::{
    CallbackDeliveryRoute as LifecycleCallbackDeliveryRoute, CallbackEffectIdentity,
    ChildAdmissionOutcome, ChildAdmissionRecord, ChildReadySetIdentity, ContinuationDispatchRecord,
    ContinuationDispatchState, ContinuationWriteOutcome, DirectDeliveryResultKind,
    DurableCallbackRecord, IntakeOutcome, LifecycleAdmissionProjection,
    LifecycleCallbackProjection, LifecycleConfig, LiveEffectEvidence, ReclaimOutcome,
    SessionLifecycleStore, TerminalReceipt, TerminalReceiptIdentity, TerminalState,
    canonical_value_sha256, commander_store_path,
};
#[cfg(test)]
use session_log_contract::SessionMetadata;
use session_log_contract::{
    ActivateRuntimeLeaseRequest, CreateSessionRequest, ExecuteSessionCommandRequest,
    GetRuntimeLeaseRequest, GetSessionRequest, RecoveryCloseRuntimeOutcome,
    RecoveryCloseRuntimeReason, RecoveryCloseRuntimeRequest, RegisterRuntimeRequest,
    ReplayRuntimeRequest, RuntimeLeaseOutcome, RuntimeLeaseSnapshot, RuntimeLifecycleIdentity,
    RuntimeRecoveryQuiescenceProof, RuntimeRecoveryReceipt, RuntimeRegistrationOutcome,
    SessionFeedEntry, SessionFeedEvent, SessionLogCommand, SessionLogResponse, SessionSnapshot,
    recovery_terminal_projection_event_id,
};
use tura_path::jspace::{JSpaceMatcher, JSpaceScopeProjection, scope_projections_conflict};

#[derive(Clone)]
pub struct ExecutionService {
    admission: Arc<RwLock<()>>,
    admission_reservation: Arc<tokio::sync::Mutex<()>>,
    sessions: Arc<Mutex<HashMap<String, RuntimeLease>>>,
    runtime_slots: RuntimeSlotGate,
    retained_slots: Arc<Mutex<HashMap<String, RuntimeSlotPermit>>>,
    retained_watchers: Arc<Mutex<HashMap<String, Arc<Notify>>>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct RuntimeLease {
    runtime_id: RuntimeId,
    lease_id: String,
    commander_session_id: String,
    transaction_id: String,
    parent_mission_revision_sha256: Option<String>,
    delegated_input_sha256: Option<String>,
    task_id: Option<String>,
    goal_id: Option<String>,
    operator_override: bool,
    receipt_event_seq: u64,
    slot_acquired: bool,
    terminalizing: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct CommanderConvergenceRecoveryEvidence {
    proof_sha256: String,
}

fn active_turn_conflict(session_id: &str, active: &RuntimeLease) -> Value {
    json!({
        "ok": false,
        "code": "session_active_turn",
        "session_id": session_id,
        "runtime_id": active.runtime_id,
        "error": format!("session {session_id} already has an active turn"),
    })
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RouterRecoveryCloseRuntimeRequest {
    receipt_id: String,
    database_path: String,
    runtime_id: String,
    session_id: String,
    lease_id: Option<String>,
    expected_lease_active: bool,
    expected_revision: u64,
    expected_last_event_seq: u64,
    expected_session_event_seq: u64,
    expected_session_state: lifecycle::SessionState,
    reason: RecoveryCloseRuntimeReason,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct TerminalDeliveryIdentity {
    pub(crate) commander_session_id: String,
    pub(crate) transaction_id: String,
    pub(crate) event_id: String,
    pub(crate) runtime_id: String,
    pub(crate) callback_payload_sha256: Option<String>,
    pub(crate) callback_effect_identity: Option<CallbackEffectIdentity>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DurableChildAdmissionDisposition {
    NotAdmitted,
    Admitted,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DurableChildCompletionState {
    EmptyPlanPending,
    SimpleEmptyPlanTerminal,
    TaskManagedPending,
    TaskManagedTerminal,
}

impl ExecutionService {
    pub fn new() -> Self {
        Self {
            admission: Arc::new(RwLock::new(())),
            admission_reservation: Arc::new(tokio::sync::Mutex::new(())),
            sessions: Arc::new(Mutex::new(HashMap::new())),
            runtime_slots: RuntimeSlotGate::default(),
            retained_slots: Arc::new(Mutex::new(HashMap::new())),
            retained_watchers: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub async fn enqueue_turn_request(
        &self,
        state: &AppState,
        input: Value,
        request_id: &str,
    ) -> Result<Value> {
        if input
            .get("payload")
            .and_then(|payload| payload.get("native_codex_execution"))
            .is_some_and(|binding| !binding.is_null())
        {
            return Err(anyhow!("NATIVE_CODEX_COMMANDER_ADMISSION_REQUIRED"));
        }
        let lease_id = format!("lease-{}", uuid::Uuid::new_v4());
        self.enqueue_turn_request_with_identity(state, input, request_id, lease_id, None, None)
            .await
    }

    pub fn commander_task_packet_capabilities(&self) -> Result<Value> {
        serde_json::to_value(CommanderTaskPacketCapabilities {
            schema_version: "tura_commander_task_packet_capabilities_v1".to_string(),
            protocol_version: COMMANDER_DISPATCH_PROTOCOL_VERSION.to_string(),
            task_packet_schema_versions: vec![COMMANDER_TASK_PACKET_SCHEMA_VERSION.to_string()],
            callback_delivery_route: WireCallbackDeliveryRoute::TrustedTuraDirectThreadWriter,
            compile_only: true,
            idempotent_replay: true,
        })
        .map_err(Into::into)
    }

    pub async fn compile_commander_task_packet_request(
        &self,
        state: &AppState,
        input: Value,
    ) -> Result<Value> {
        let compiled = self
            .prepare_commander_task_packet_dispatch(state, input)
            .await?;
        serde_json::to_value(compiled.result).map_err(Into::into)
    }

    pub(crate) async fn prepare_commander_task_packet_dispatch(
        &self,
        state: &AppState,
        input: Value,
    ) -> Result<CompiledCommanderTaskPacket> {
        let packet: CommanderTaskPacketV1 = serde_json::from_value(input).map_err(|error| {
            anyhow!("TASK_PACKET_PRE_ADMISSION:TASK_PACKET_WIRE_INVALID:{error}")
        })?;
        packet
            .validate_shape()
            .map_err(|error| anyhow!("TASK_PACKET_PRE_ADMISSION:{error}"))?;
        state.session_db.start().map_err(|error| {
            anyhow!("TASK_PACKET_PRE_ADMISSION:TASK_PACKET_SESSION_DB_UNAVAILABLE:{error}")
        })?;
        let parent = read_session_snapshot(&packet.parent_session_id)
            .map_err(|error| anyhow!("TASK_PACKET_PRE_ADMISSION:{error}"))?
            .ok_or_else(|| {
                anyhow!(
                    "TASK_PACKET_PRE_ADMISSION:TASK_PACKET_PARENT_SESSION_NOT_FOUND:{}",
                    packet.parent_session_id
                )
            })?;
        validate_commander_execution_authority(state, &packet, &parent)
            .await
            .map_err(|error| anyhow!("TASK_PACKET_PRE_ADMISSION:{error}"))?;
        compile_commander_task_packet(&packet, &parent)
            .map_err(|error| anyhow!("TASK_PACKET_PRE_ADMISSION:{error}"))
    }

    pub async fn dispatch_commander_task_packet_request(
        &self,
        state: &AppState,
        input: Value,
    ) -> Result<Value> {
        let compiled = self
            .prepare_commander_task_packet_dispatch(state, input)
            .await?;
        self.dispatch_prepared_commander_task_packet(state, compiled)
            .await
    }

    pub(crate) async fn dispatch_prepared_commander_task_packet(
        &self,
        state: &AppState,
        compiled: CompiledCommanderTaskPacket,
    ) -> Result<Value> {
        let expected_parent_task_plan_sha256 = compiled.result.parent_task_plan_sha256.clone();
        let admission: RegisterChildSessionResponse = serde_json::from_value(
            self.register_child_session_request_with_prepared_parent_plan(
                state,
                serde_json::to_value(&compiled.request)?,
                Some(&expected_parent_task_plan_sha256),
            )
            .await
            .map_err(|error| {
                if let Some(preclaim) = error.downcast_ref::<PreClaimChildAdmissionError>() {
                    anyhow!("TASK_PACKET_PRE_ADMISSION:{preclaim}")
                } else {
                    anyhow!("TASK_PACKET_DISPATCH_FAILED:{error}")
                }
            })?,
        )?;
        serde_json::to_value(CommanderTaskPacketDispatchResponse {
            schema_version: "tura_commander_task_packet_dispatch_response_v1".to_string(),
            compilation: compiled.result,
            admission,
            duplicate_effect_count: 0,
            commander_mission_verification_required: false,
        })
        .map_err(Into::into)
    }

    pub(crate) fn durable_child_admission_disposition(
        &self,
        request: &RegisterChildSessionRequest,
    ) -> Result<DurableChildAdmissionDisposition> {
        let store = lifecycle_store(&request.parent_session_id)?;
        durable_child_admission_disposition_from_store(&store, request)
    }

    pub(crate) fn durable_admitted_child_completion(
        &self,
        commander_session_id: &str,
        child_session_id: &str,
        runtime_id: &str,
        transaction_id: &str,
    ) -> Result<DurableChildCompletionState> {
        let store = lifecycle_store(commander_session_id)?;
        let admission = store.child_admission(child_session_id)?.ok_or_else(|| {
            anyhow!("TERMINAL_CALLBACK_CHILD_ADMISSION_NOT_DURABLE:{child_session_id}")
        })?;
        if admission.parent_session_id != commander_session_id
            || admission.child_session_id != child_session_id
            || admission.child_runtime_id != runtime_id
            || admission.child_transaction_id != transaction_id
        {
            return Err(anyhow!(
                "TERMINAL_CALLBACK_ADMISSION_IDENTITY_MISMATCH:{child_session_id}"
            ));
        }
        let child = read_session_snapshot(child_session_id)?.ok_or_else(|| {
            anyhow!("TERMINAL_CALLBACK_CHILD_SESSION_NOT_FOUND:{child_session_id}")
        })?;
        ensure_child_parent_identity(&child, commander_session_id)?;
        Ok(child_completion_state(
            child.lifecycle_projection.state,
            &child.lifecycle_projection.task_plan,
        ))
    }

    pub(crate) fn replay_admitted_pre_execution_failure_callback(
        &self,
        request: &RegisterChildSessionRequest,
    ) -> Result<Option<(Value, TerminalDeliveryIdentity)>> {
        require_admitted_pre_execution_child_terminal(request)?;
        let expected_event_id = admitted_pre_execution_failure_event_id(&request.child_runtime_id);
        let mut matching = self
            .replay_terminal_callbacks(
                &request.parent_session_id,
                &request.child_session_id,
                &request.child_transaction_id,
            )?
            .into_iter()
            .filter(|(_, delivery)| delivery.event_id == expected_event_id);
        let callback = matching.next();
        if matching.next().is_some() {
            return Err(anyhow!(
                "ADMITTED_PRE_EXECUTION_CALLBACK_REPLAY_CONFLICT:{}",
                request.child_session_id
            ));
        }
        Ok(callback)
    }

    pub async fn register_child_session_request(
        &self,
        state: &AppState,
        input: Value,
    ) -> Result<Value> {
        self.register_child_session_request_with_prepared_parent_plan(state, input, None)
            .await
    }

    async fn register_child_session_request_with_prepared_parent_plan(
        &self,
        state: &AppState,
        input: Value,
        expected_parent_task_plan_sha256: Option<&str>,
    ) -> Result<Value> {
        let request: RegisterChildSessionRequest = serde_json::from_value(input)?;
        request.validate().map_err(anyhow::Error::msg)?;
        let mut payload = validated_child_execution_payload(&request)?;
        state.session_db.start()?;

        let replay_store = lifecycle_store(&request.parent_session_id)?;
        if let Some(existing) = replay_store.child_admission(&request.child_session_id)? {
            validate_existing_child_admission(&existing, &request)?;
            let ready_set_identity = existing
                .ready_set_identity
                .as_ref()
                .ok_or_else(|| anyhow!("CHILD_READY_SET_LEGACY_ADMISSION_NOT_REPLAYABLE"))?;
            validate_prepared_parent_task_plan_identity(
                expected_parent_task_plan_sha256,
                &ready_set_identity.parent_task_plan_sha256,
            )?;
            if admitted_pre_execution_failure_receipt_from_store(
                &replay_store,
                &request,
                &ready_set_identity.task_id,
            )?
            .is_some()
            {
                require_admitted_pre_execution_child_terminal(&request)?;
                publish_admitted_pre_execution_failure_callback_from_store(
                    &replay_store,
                    &request,
                    &ready_set_identity.task_id,
                    None,
                )?;
                return register_child_session_response(
                    request,
                    RegisterChildSessionOutcome::AlreadyAdmitted,
                );
            }
            match child_runtime_registration_state(&request, &ready_set_identity.task_id)? {
                ChildRuntimeRegistrationState::Active | ChildRuntimeRegistrationState::Terminal => {
                    return register_child_session_response(
                        request,
                        RegisterChildSessionOutcome::AlreadyAdmitted,
                    );
                }
                ChildRuntimeRegistrationState::Absent | ChildRuntimeRegistrationState::Unborn => {}
            }
        }

        let admission_guard = Arc::clone(&self.admission_reservation).lock_owned().await;

        let parent = read_session_snapshot(&request.parent_session_id)?.ok_or_else(|| {
            anyhow!(
                "CHILD_ADMISSION_PARENT_SESSION_NOT_FOUND:{}",
                request.parent_session_id
            )
        })?;
        let existing_child = read_session_snapshot(&request.child_session_id)?;
        let store = lifecycle_store(&request.parent_session_id)?;
        if existing_child.is_some() && store.child_admission(&request.child_session_id)?.is_none() {
            return Err(anyhow!(
                "CHILD_ADMISSION_EXISTING_CHILD_WITHOUT_IDENTITY:{}",
                request.child_session_id
            ));
        }

        let ready_set_identity = validate_child_ready_set_binding(
            self,
            &parent,
            &request,
            &payload,
            expected_parent_task_plan_sha256,
        )?;
        bind_child_task_identity(&mut payload, &ready_set_identity.task_id)?;
        let record = child_admission_record(&request, ready_set_identity.clone());
        if record.ready_set_identity.as_ref() != Some(&ready_set_identity) {
            return Err(anyhow!("CHILD_READY_SET_DURABLE_IDENTITY_MISMATCH"));
        }
        let admitted = store.admit_child(&record)?;
        let child_creation = match existing_child {
            Some(child) => ensure_child_parent_identity(&child, &request.parent_session_id),
            None => create_child_session(&parent, &request),
        };
        if let Err(error) = child_creation {
            self.terminalize_admitted_child_pre_execution_failure(
                state,
                &request,
                &ready_set_identity.task_id,
                &error,
            )
            .await
            .map_err(|terminalization_error| {
                anyhow!(
                    "CHILD_ADMISSION_PRE_EXECUTION_TERMINALIZATION_FAILED:{error:#}:{terminalization_error:#}"
                )
            })?;
            return Err(error);
        }
        if durable_child_admission_disposition_from_store(&store, &request)?
            != DurableChildAdmissionDisposition::Admitted
        {
            return Err(anyhow!(
                "CHILD_ADMISSION_DURABLE_READBACK_MISSING:{}",
                request.child_session_id
            ));
        }

        match child_runtime_registration_state(&request, &ready_set_identity.task_id)? {
            ChildRuntimeRegistrationState::Absent | ChildRuntimeRegistrationState::Unborn => {
                let response = match self
                    .enqueue_turn_request_with_identity(
                        state,
                        serde_json::to_value(EnqueueTurnRequest {
                            runtime_id: request.child_runtime_id.clone(),
                            session_id: request.child_session_id.clone(),
                            payload,
                        })?,
                        &request.child_transaction_id,
                        request.child_lease_id.clone(),
                        None,
                        Some(admission_guard),
                    )
                    .await
                {
                    Ok(response) => response,
                    Err(error) => {
                        self.terminalize_admitted_child_pre_execution_failure(
                            state,
                            &request,
                            &ready_set_identity.task_id,
                            &error,
                        )
                        .await
                        .map_err(|terminalization_error| {
                            anyhow!(
                                "CHILD_ADMISSION_PRE_EXECUTION_TERMINALIZATION_FAILED:{error:#}:{terminalization_error:#}"
                            )
                        })?;
                        return Err(error);
                    }
                };
                if response.get("ok").and_then(Value::as_bool) == Some(false) {
                    let error = anyhow!(
                        "CHILD_ADMISSION_EXECUTION_REJECTED:{}",
                        response
                            .get("error")
                            .and_then(Value::as_str)
                            .unwrap_or("router execution rejected")
                    );
                    self.terminalize_admitted_child_pre_execution_failure(
                        state,
                        &request,
                        &ready_set_identity.task_id,
                        &error,
                    )
                    .await
                    .map_err(|terminalization_error| {
                        anyhow!(
                            "CHILD_ADMISSION_PRE_EXECUTION_TERMINALIZATION_FAILED:{error:#}:{terminalization_error:#}"
                        )
                    })?;
                    return Err(error);
                }
            }
            ChildRuntimeRegistrationState::Active | ChildRuntimeRegistrationState::Terminal => {
                drop(admission_guard);
            }
        }

        register_child_session_response(
            request,
            match admitted {
                ChildAdmissionOutcome::Admitted => RegisterChildSessionOutcome::Admitted,
                ChildAdmissionOutcome::AlreadyAdmitted => {
                    RegisterChildSessionOutcome::AlreadyAdmitted
                }
            },
        )
    }

    async fn terminalize_admitted_child_pre_execution_failure(
        &self,
        state: &AppState,
        request: &RegisterChildSessionRequest,
        task_id: &str,
        failure: &anyhow::Error,
    ) -> Result<()> {
        let store = lifecycle_store(&request.parent_session_id)?;
        if durable_child_admission_disposition_from_store(&store, request)?
            != DurableChildAdmissionDisposition::Admitted
        {
            return Err(anyhow!(
                "ADMITTED_PRE_EXECUTION_FAILURE_ADMISSION_READBACK_MISSING:{}",
                request.child_session_id
            ));
        }
        match child_runtime_registration_state(request, task_id)? {
            ChildRuntimeRegistrationState::Active | ChildRuntimeRegistrationState::Terminal => {
                Ok(())
            }
            ChildRuntimeRegistrationState::Absent => {
                cancel_unstarted_child_session(request)?;
                require_admitted_pre_execution_child_terminal(request)?;
                publish_admitted_pre_execution_failure_callback_from_store(
                    &store,
                    request,
                    task_id,
                    Some(failure),
                )?;
                Ok(())
            }
            ChildRuntimeRegistrationState::Unborn => {
                let lease = RuntimeLease {
                    runtime_id: request.child_runtime_id.clone(),
                    lease_id: request.child_lease_id.clone(),
                    commander_session_id: request.parent_session_id.clone(),
                    transaction_id: request.child_transaction_id.clone(),
                    parent_mission_revision_sha256: Some(
                        request.parent_mission_revision_sha256.clone(),
                    ),
                    delegated_input_sha256: Some(request.delegated_input_sha256.clone()),
                    task_id: Some(task_id.to_string()),
                    goal_id: None,
                    operator_override: false,
                    receipt_event_seq: 0,
                    slot_acquired: false,
                    terminalizing: false,
                };
                {
                    let mut sessions = self.sessions.lock();
                    if let Some(existing) = sessions.get(&request.child_session_id) {
                        if existing != &lease {
                            return Err(anyhow!(
                                "ADMITTED_PRE_EXECUTION_FAILURE_LEASE_CONFLICT:{}",
                                request.child_session_id
                            ));
                        }
                    } else {
                        sessions.insert(request.child_session_id.clone(), lease.clone());
                    }
                }
                let active_guard = ActiveSessionGuard::new(
                    Arc::clone(&self.sessions),
                    &request.child_session_id,
                    &request.child_runtime_id,
                );
                register_and_activate_runtime(
                    &request.child_session_id,
                    &request.child_runtime_id,
                    &request.child_lease_id,
                    None,
                    Some(RuntimeLifecycleIdentity {
                        commander_session_id: request.parent_session_id.clone(),
                        transaction_id: request.child_transaction_id.clone(),
                        parent_mission_revision_sha256: Some(
                            request.parent_mission_revision_sha256.clone(),
                        ),
                        delegated_input_sha256: Some(request.delegated_input_sha256.clone()),
                        task_id: Some(task_id.to_string()),
                        goal_id: None,
                        operator_override: false,
                        dispatch_runtime_id: request.child_runtime_id.clone(),
                        dispatch_lease_id: request.child_lease_id.clone(),
                        receipt_event_seq: 0,
                    }),
                )?;
                self.terminalize_registered_runtime(
                    state,
                    &request.child_session_id,
                    &request.child_runtime_id,
                    None,
                )
                .await?;
                active_guard.finish();
                Ok(())
            }
        }
    }

    async fn enqueue_turn_request_with_identity(
        &self,
        state: &AppState,
        input: Value,
        request_id: &str,
        lease_id: String,
        continuation: Option<ContinuationDispatchRecord>,
        admission_reservation: Option<tokio::sync::OwnedMutexGuard<()>>,
    ) -> Result<Value> {
        let request: EnqueueTurnRequest = serde_json::from_value(input)?;
        let mut admission_reservation = Some(match admission_reservation {
            Some(guard) => guard,
            None => Arc::clone(&self.admission_reservation).lock_owned().await,
        });
        let _admission = self.admission.read().await;
        if let Some(active) = self.sessions.lock().get(&request.session_id) {
            return Ok(active_turn_conflict(&request.session_id, active));
        }
        state.session_db.start()?;
        let requested_continuation_fallback = request
            .payload
            .get("fallback_from_id")
            .and_then(Value::as_str)
            .map(str::to_string);
        if requested_continuation_fallback.is_some() && continuation.is_none() {
            return Err(anyhow!(
                "CALLBACK_CONTINUATION_FALLBACK_WITHOUT_DURABLE_RECORD:{}",
                request.runtime_id
            ));
        }
        if let Some(fallback_from_id) = requested_continuation_fallback.as_deref() {
            validate_continuation_fallback_source(&request.session_id, fallback_from_id)?;
        }
        let mut run_request = payload_to_run_agent_request(
            &request,
            &lease_id,
            requested_continuation_fallback.clone(),
        )?;
        let requested_prompt = run_request.effective_prompt();
        let fallback_from_id = match requested_continuation_fallback {
            Some(fallback_from_id) => Some(fallback_from_id),
            None => runtime_registration_fallback(&request.session_id, requested_prompt)?,
        };
        run_request.fallback_from_id.clone_from(&fallback_from_id);
        let commander_session_id = run_request
            .parent_session_id
            .clone()
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| request.session_id.clone());
        let delegated = run_request
            .parent_session_id
            .as_deref()
            .is_some_and(|value| !value.trim().is_empty());
        validate_delegated_input_digest(&mut run_request, delegated)?;
        run_request
            .validate_delegated_identity()
            .map_err(anyhow::Error::msg)?;
        let supplied_lifecycle = run_request.lifecycle.take();
        let task_id = supplied_lifecycle
            .as_ref()
            .and_then(|context| context.task_id.clone())
            .or_else(|| run_request.task_id.clone());
        let goal_id = supplied_lifecycle
            .as_ref()
            .and_then(|context| context.goal_id.clone())
            .or_else(|| run_request.goal_id.clone());
        let operator_override = supplied_lifecycle
            .as_ref()
            .map(|context| context.operator_override)
            .unwrap_or(run_request.operator_override);
        run_request.lifecycle = Some(LifecycleExecutionContext {
            transaction_id: request_id.to_string(),
            commander_session_id: commander_session_id.clone(),
            parent_mission_revision_sha256: run_request.parent_mission_revision_sha256.clone(),
            delegated_input_sha256: run_request.delegated_input_sha256.clone(),
            task_id: task_id.clone(),
            goal_id: goal_id.clone(),
            operator_override,
            commander_continuation: supplied_lifecycle
                .as_ref()
                .and_then(|context| context.commander_continuation.clone()),
        });
        let durable_lifecycle = RuntimeLifecycleIdentity {
            commander_session_id: commander_session_id.clone(),
            transaction_id: request_id.to_string(),
            parent_mission_revision_sha256: run_request.parent_mission_revision_sha256.clone(),
            delegated_input_sha256: run_request.delegated_input_sha256.clone(),
            task_id: task_id.clone(),
            goal_id: goal_id.clone(),
            operator_override,
            dispatch_runtime_id: request.runtime_id.clone(),
            dispatch_lease_id: lease_id.clone(),
            receipt_event_seq: 0,
        };
        let maximum_parallel_runtime_workers =
            runtime_worker_limit(run_request.maximum_parallel_runtime_workers);
        if debug_runtime_enabled() {
            eprintln!(
                "router debug: enqueue_turn start session_id={} runtime_id={}",
                request.session_id, request.runtime_id
            );
        }
        {
            let mut sessions = self.sessions.lock();
            if let Some(active) = sessions.get(&request.session_id) {
                return Ok(active_turn_conflict(&request.session_id, active));
            }
            let queued = sessions
                .values()
                .filter(|lease| !lease.slot_acquired && !lease.terminalizing)
                .count();
            if queued >= MAX_QUEUED_RUNTIME_TURNS {
                return Err(anyhow!(
                    "runtime turn queue is full ({queued}/{MAX_QUEUED_RUNTIME_TURNS})"
                ));
            }
            sessions.insert(
                request.session_id.clone(),
                RuntimeLease {
                    runtime_id: request.runtime_id.clone(),
                    lease_id: lease_id.clone(),
                    commander_session_id,
                    transaction_id: request_id.to_string(),
                    parent_mission_revision_sha256: run_request
                        .parent_mission_revision_sha256
                        .clone(),
                    delegated_input_sha256: run_request.delegated_input_sha256.clone(),
                    task_id,
                    goal_id,
                    operator_override,
                    receipt_event_seq: 0,
                    slot_acquired: false,
                    terminalizing: false,
                },
            );
        }
        let active_guard = ActiveSessionGuard::new(
            Arc::clone(&self.sessions),
            &request.session_id,
            &request.runtime_id,
        );
        drop(admission_reservation.take());
        let permit = self
            .acquire_runtime_slot(&request.session_id, maximum_parallel_runtime_workers)
            .await?;
        if !self.mark_slot_acquired(&request.session_id, &request.runtime_id) {
            return Err(anyhow!(
                "session {} was cancelled before runtime dispatch",
                request.session_id
            ));
        }
        register_and_activate_runtime(
            &request.session_id,
            &request.runtime_id,
            &lease_id,
            fallback_from_id,
            Some(durable_lifecycle),
        )?;
        if let Some(record) = continuation.as_ref() {
            let store = lifecycle_store(&record.commander_session_id)?;
            store.mark_callback_continuation_dispatched(record)?;
        }
        if debug_runtime_enabled() {
            eprintln!(
                "router debug: enqueue_turn dispatch session_id={}",
                request.session_id
            );
        }
        let (status, body) =
            dispatch_run_agent_with_runtime_slot(state, run_request, request_id.to_string()).await;
        let delivery = match self.ensure_terminal_receipt(
            &request.session_id,
            &request.runtime_id,
            request_id,
        ) {
            Ok(delivery) => delivery,
            Err(error) => {
                let missing_terminal_feed = error
                    .to_string()
                    .starts_with("TERMINAL_FEED_EVENT_NOT_FOUND:");
                if missing_terminal_feed {
                    let convergence_recovery = continuation
                        .as_ref()
                        .and_then(|record| commander_continuation_binding(record).ok())
                        .and_then(|binding| {
                            read_session_snapshot(&request.session_id)
                                .ok()
                                .flatten()
                                .and_then(|snapshot| {
                                    terminal_commander_convergence_recovery_evidence(
                                        std::path::Path::new(&snapshot.metadata.session_directory),
                                        &request.session_id,
                                        &request.runtime_id,
                                        &binding,
                                    )
                                    .ok()
                                    .flatten()
                                })
                        });
                    match self
                        .terminalize_registered_runtime(
                            state,
                            &request.session_id,
                            &request.runtime_id,
                            convergence_recovery.as_ref(),
                        )
                        .await
                    {
                        Ok(()) => match self.ensure_terminal_receipt(
                            &request.session_id,
                            &request.runtime_id,
                            request_id,
                        ) {
                            Ok(delivery) => delivery,
                            Err(receipt_error) => {
                                match self.retain_runtime_slot_if_current(
                                    &request.session_id,
                                    &request.runtime_id,
                                    permit,
                                ) {
                                    Ok(()) => active_guard.retain(),
                                    Err(permit) => {
                                        active_guard.finish();
                                        drop(permit);
                                    }
                                }
                                return Err(anyhow!(
                                    "TERMINAL_RECEIPT_NOT_DURABLE:{error:#}:RUNTIME_TERMINALIZED_BUT_RECEIPT_NOT_DURABLE:{receipt_error:#}"
                                ));
                            }
                        },
                        Err(terminalization_error) => {
                            match self.retain_runtime_slot_if_current(
                                &request.session_id,
                                &request.runtime_id,
                                permit,
                            ) {
                                Ok(()) => active_guard.retain(),
                                Err(permit) => {
                                    active_guard.finish();
                                    drop(permit);
                                }
                            }
                            return Err(anyhow!(
                                "TERMINAL_RECEIPT_NOT_DURABLE:{error:#}:AUTO_TERMINALIZATION_BLOCKED:{terminalization_error:#}"
                            ));
                        }
                    }
                } else {
                    match self.retain_runtime_slot_if_current(
                        &request.session_id,
                        &request.runtime_id,
                        permit,
                    ) {
                        Ok(()) => active_guard.retain(),
                        Err(permit) => {
                            active_guard.finish();
                            drop(permit);
                        }
                    }
                    return Err(anyhow!("TERMINAL_RECEIPT_NOT_DURABLE:{error:#}"));
                }
            }
        };
        state
            .command_run
            .wait_for_session_idle(&request.session_id)
            .await;
        let evidence = self.live_effect_evidence(state, &request.session_id).await;
        match lifecycle_store(&delivery.commander_session_id)?.reclaim_terminal_slot(
            &delivery.transaction_id,
            &delivery.event_id,
            evidence,
        )? {
            ReclaimOutcome::Released | ReclaimOutcome::AlreadyReleased => {
                active_guard.finish();
                drop(permit);
            }
            ReclaimOutcome::Retained { blocker } => {
                if let Err(permit) = self.retain_runtime_slot_if_current(
                    &request.session_id,
                    &request.runtime_id,
                    permit,
                ) {
                    active_guard.finish();
                    drop(permit);
                    return Err(anyhow!(
                        "RUNTIME_CANCELLED_BEFORE_SLOT_RETAIN:session={},runtime={}",
                        request.session_id,
                        request.runtime_id
                    ));
                }
                self.spawn_retained_reclaimer(
                    state.clone(),
                    request.session_id.clone(),
                    delivery.clone(),
                );
                self.wait_for_retained_release(&request.session_id).await;
                if self.retained_slots.lock().contains_key(&request.session_id) {
                    active_guard.retain();
                    return Err(anyhow!(blocker));
                }
                active_guard.finish();
            }
        }
        if debug_runtime_enabled() {
            eprintln!(
                "router debug: enqueue_turn finished session_id={} status={} body={}",
                request.session_id, status, body
            );
        }
        require_successful_runtime_dispatch(status, &body)?;
        if let Some(record) = continuation.as_ref() {
            let store = lifecycle_store(&record.commander_session_id)?;
            let bound =
                bind_commander_convergence_proof_from_runtime(&store, record, &request.runtime_id)?;
            if !store.callback_continuation_completion_proven(&bound)? {
                return Err(anyhow!(
                    "CONTINUATION_SUCCESS_EVIDENCE_NOT_DURABLE:{}:{}:{}",
                    record.request_id,
                    record.runtime_id,
                    record.lease_id
                ));
            }
            complete_and_ack_callback_continuation(&store, &bound)?;
        }
        Ok(json!({
            "status": "finished",
            "runtime_id": request.runtime_id,
            "session_id": request.session_id,
            "result": body
        }))
    }

    pub async fn command_run_request(
        &self,
        state: &AppState,
        input: Value,
        request_id: &str,
    ) -> Result<Value> {
        let nested_session = input
            .get("session_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty());
        let nested_reservation = {
            let sessions = self.sessions.lock();
            nested_session.and_then(|session_id| {
                sessions
                    .contains_key(session_id)
                    .then(|| state.command_run.reserve_for_session(Some(session_id)))
            })
        };
        if let Some(reservation) = nested_reservation {
            return state
                .command_run
                .execute_with_reserved_session(input, Some(request_id), reservation)
                .await;
        }

        let _admission = self.admission.read().await;
        state
            .command_run
            .execute_with_request_id(input, Some(request_id))
            .await
    }

    pub async fn get_runtime_lease(&self, state: &AppState, input: Value) -> Result<Value> {
        let request: GetRuntimeLeaseRequest = serde_json::from_value(input)?;
        state.session_db.start()?;
        match session_log_contract::client::call_service(&SessionLogCommand::GetRuntimeLease(
            request,
        ))? {
            SessionLogResponse::RuntimeLeaseRead { runtime } => Ok(json!({
                "status": "ok",
                "runtime": runtime,
            })),
            SessionLogResponse::Error { error } => Err(anyhow!(error)),
            other => Err(anyhow!("unexpected get_runtime_lease response: {other:?}")),
        }
    }

    pub fn read_control_deck_convergence(&self, input: Value) -> Result<Value> {
        let mut request: ReadControlDeckConvergenceRequest = serde_json::from_value(input)?;
        request.validate().map_err(anyhow::Error::msg)?;
        request.commander_session_ids.sort();
        request.commander_session_ids.dedup();
        let mut entries = Vec::new();
        for commander_session_id in request.commander_session_ids {
            match lifecycle_store_if_exists(&commander_session_id) {
                Ok(Some(store)) => match store.control_deck_projection() {
                    Ok(projection) => entries.push(ControlDeckConvergenceEntry {
                        commander_session_id,
                        availability: ControlDeckConvergenceAvailability::Present,
                        projection: Some(serde_json::to_value(projection)?),
                        blocker_code: None,
                    }),
                    Err(error) => entries.push(ControlDeckConvergenceEntry {
                        commander_session_id,
                        availability: ControlDeckConvergenceAvailability::Unavailable,
                        projection: None,
                        blocker_code: Some(error.code),
                    }),
                },
                Ok(None) => entries.push(ControlDeckConvergenceEntry {
                    commander_session_id,
                    availability: ControlDeckConvergenceAvailability::TypedAbsence,
                    projection: None,
                    blocker_code: Some("LIFECYCLE_STORE_ABSENT".to_string()),
                }),
                Err(error) => entries.push(ControlDeckConvergenceEntry {
                    commander_session_id,
                    availability: ControlDeckConvergenceAvailability::Unavailable,
                    projection: None,
                    blocker_code: Some(
                        error
                            .to_string()
                            .split(':')
                            .next()
                            .unwrap_or("LIFECYCLE_STORE_UNAVAILABLE")
                            .to_string(),
                    ),
                }),
            }
        }
        Ok(serde_json::to_value(ReadControlDeckConvergenceResponse {
            schema_version: "tura_control_deck_convergence_response_v1".to_string(),
            entries,
        })?)
    }

    pub fn read_task_ready_set(&self, state: &AppState, input: Value) -> Result<Value> {
        let request: ReadTaskReadySetRequest = serde_json::from_value(input)?;
        request.validate().map_err(anyhow::Error::msg)?;
        state.session_db.start()?;
        serde_json::to_value(evaluate_task_ready_set_request(self, &request)?).map_err(Into::into)
    }

    pub(crate) fn acknowledge_child_callback_request(&self, input: Value) -> Result<Value> {
        let request: AcknowledgeChildCallbackRequest = serde_json::from_value(input)?;
        request.validate().map_err(anyhow::Error::msg)?;
        let store = lifecycle_store(&request.parent_session_id)?;
        acknowledge_child_callback_from_store(&store, request)
    }

    pub(crate) fn reconcile_durable_terminal_callback(
        &self,
        snapshot: &RuntimeLeaseSnapshot,
    ) -> Result<Option<TerminalDeliveryIdentity>> {
        if !snapshot.terminal || snapshot.lease_active {
            return Err(anyhow!(
                "RUNTIME_CALLBACK_RECONCILIATION_REQUIRES_CLOSED_RUNTIME:runtime={},terminal={},lease_active={}",
                snapshot.runtime_id,
                snapshot.terminal,
                snapshot.lease_active
            ));
        }
        let lease = runtime_lease_from_snapshot(snapshot)?;
        let entry = terminal_feed_entry_for_runtime(&snapshot.session_id, &snapshot.runtime_id)?;
        let SessionFeedEvent::SessionProjectionUpdated { projection, .. } = &entry.event else {
            return Err(anyhow!(
                "RUNTIME_CALLBACK_TERMINAL_PROJECTION_MISSING:{}",
                snapshot.runtime_id
            ));
        };
        if !terminal_runtime_is_current(
            projection,
            &snapshot.session_id,
            &lease.runtime_id,
            &snapshot.runtime_id,
        )? {
            return Ok(None);
        }
        let expected_terminal_state = runtime_terminal_state_from_snapshot(snapshot, projection)?;
        self.write_snapshot_terminal_receipt(&lease, snapshot, &entry, expected_terminal_state)?;
        let store = lifecycle_store(&lease.commander_session_id)?;
        let mut delivery = intake_terminal_receipt(
            &store,
            &entry,
            &snapshot.runtime_id,
            &lease.transaction_id,
            &lease,
            expected_terminal_state,
        )?;
        if let Some(delivery) = delivery.as_ref() {
            match store.reclaim_terminal_slot(
                &delivery.transaction_id,
                &delivery.event_id,
                LiveEffectEvidence::default(),
            )? {
                ReclaimOutcome::Released | ReclaimOutcome::AlreadyReleased => {}
                ReclaimOutcome::Retained { blocker } => return Err(anyhow!(blocker)),
            }
        }
        if let Some(current) = delivery.take() {
            delivery = match publish_terminal_failure_callback_from_store(&store, current.clone())?
            {
                Some((_transport, failure_delivery)) => Some(failure_delivery),
                None => Some(current),
            };
        }
        Ok(delivery)
    }

    pub(crate) fn historical_terminal_state_mismatch(
        &self,
        snapshot: &RuntimeLeaseSnapshot,
    ) -> Result<bool> {
        let entry = terminal_feed_entry_for_runtime(&snapshot.session_id, &snapshot.runtime_id)?;
        let SessionFeedEvent::SessionProjectionUpdated { projection, .. } = entry.event else {
            return Ok(false);
        };
        Ok(is_historical_terminal_runtime(
            projection.active_runtime_id.as_deref(),
            &snapshot.runtime_id,
        ))
    }

    pub async fn recovery_close_runtime(&self, state: &AppState, input: Value) -> Result<Value> {
        let request: RouterRecoveryCloseRuntimeRequest = serde_json::from_value(input)?;
        let _admission = self.admission.write().await;

        let lease = self.sessions.lock().get(&request.session_id).cloned();
        let queued_turn = lease
            .as_ref()
            .is_some_and(|lease| !lease.slot_acquired && !lease.terminalizing);
        let running_turn = lease
            .as_ref()
            .is_some_and(|lease| lease.slot_acquired && !lease.terminalizing);
        let active_turn = lease.as_ref().is_some_and(|lease| !lease.terminalizing);
        let worker_alive = state
            .manager
            .worker_alive_by_key(&format!("runtime_worker:{}", request.session_id))
            .await;
        let retained_process_scopes =
            code_tools::shell_executor::retained_shell_process_scope_count_for_scope(
                &request.session_id,
            );
        let retained_slot = self.retained_slots.lock().contains_key(&request.session_id);
        let proof = RuntimeRecoveryQuiescenceProof {
            active_turn,
            queued_turn,
            running_turn,
            worker_alive,
            active_command_runs: state
                .command_run
                .active_count_for_session(&request.session_id)
                as u64,
            retained_process_scopes: retained_process_scopes as u64,
            retained_slot,
            global_active_session_count: self.sessions.lock().len() as u64,
            global_retained_slot_count: self.retained_slots.lock().len() as u64,
            global_active_command_runs: state.command_run.active_count() as u64,
        };
        if !proof.is_quiescent() {
            return Ok(json!({
                "status": "ok",
                "result": session_log_contract::RecoveryCloseRuntimeOutcome::RuntimeLive {
                    proof,
                },
            }));
        }

        state.session_db.start()?;
        let convergence_recovery = if request.reason == RecoveryCloseRuntimeReason::OrphanedRuntime
        {
            let matching = lifecycle_store_if_exists(&request.session_id)?
                .map(|store| store.callback_continuations_for_replay())
                .transpose()?
                .unwrap_or_default()
                .into_iter()
                .filter(|record| {
                    record.runtime_id == request.runtime_id
                        && record.state == ContinuationDispatchState::Dispatched
                        && record.commander_thread_id.is_some()
                })
                .collect::<Vec<_>>();
            if matching.len() > 1 {
                return Err(anyhow!(
                    "COMMANDER_CONVERGENCE_STARTUP_BINDING_CONFLICT:{}",
                    request.runtime_id
                ));
            }
            match matching.first() {
                Some(record) => {
                    let binding = commander_continuation_binding(record)?;
                    match read_session_snapshot(&request.session_id)? {
                        Some(snapshot) => terminal_commander_convergence_recovery_evidence(
                            std::path::Path::new(&snapshot.metadata.session_directory),
                            &request.session_id,
                            &request.runtime_id,
                            &binding,
                        )?,
                        None => None,
                    }
                }
                None => None,
            }
        } else {
            None
        };
        let recovery = RecoveryCloseRuntimeRequest {
            receipt_id: request.receipt_id,
            database_path: request.database_path,
            runtime_id: request.runtime_id,
            session_id: request.session_id,
            lease_id: request.lease_id,
            expected_lease_active: request.expected_lease_active,
            expected_revision: request.expected_revision,
            expected_last_event_seq: request.expected_last_event_seq,
            expected_session_event_seq: request.expected_session_event_seq,
            expected_session_state: request.expected_session_state,
            reason: if convergence_recovery.is_some() {
                RecoveryCloseRuntimeReason::CommanderConvergenceProven
            } else {
                request.reason
            },
            convergence_proof_sha256: convergence_recovery.map(|evidence| evidence.proof_sha256),
            quiescence: proof,
        };
        match session_log_contract::client::call_service(&SessionLogCommand::RecoveryCloseRuntime(
            recovery,
        ))? {
            SessionLogResponse::RuntimeRecoveryClosed { result } => Ok(json!({
                "status": "ok",
                "result": result,
            })),
            SessionLogResponse::Error { error } => Err(anyhow!(error)),
            other => Err(anyhow!(
                "unexpected recovery_close_runtime response: {other:?}"
            )),
        }
    }

    pub async fn cancel_turn(&self, state: &AppState, input: Value) -> Value {
        let request = match serde_json::from_value::<CancelRuntimeRequest>(input) {
            Ok(request)
                if !request.session_id.trim().is_empty()
                    && !request.runtime_id.trim().is_empty() =>
            {
                request
            }
            Ok(_) => {
                return json!({
                    "status": "error",
                    "error": "session_id and runtime_id must be non-empty",
                    "stopped_worker": false,
                });
            }
            Err(error) => {
                return json!({
                    "status": "error",
                    "error": format!("invalid cancel runtime request: {error}"),
                    "stopped_worker": false,
                });
            }
        };
        let session_id = request.session_id;
        let runtime_id = request.runtime_id;
        let lease = self
            .sessions
            .lock()
            .get(&session_id)
            .filter(|lease| lease.runtime_id == runtime_id)
            .cloned();
        let Some(lease) = lease else {
            return json!({
                "status": "idle",
                "session_id": session_id,
                "runtime_id": runtime_id,
                "stopped_worker": false,
                "active_command_runs_cancelled": 0,
            });
        };
        if let Err(error) = self.mark_terminalizing(&session_id, &runtime_id) {
            return json!({
                "status": "error",
                "session_id": session_id,
                "runtime_id": runtime_id,
                "stopped_worker": false,
                "active_command_runs_cancelled": 0,
                "runtime_terminalized": false,
                "terminalization_pending": false,
                "terminalization_error": error.to_string(),
            });
        }
        let stopped_worker = state
            .manager
            .stop_worker_by_key(&format!("runtime_worker:{session_id}"))
            .await;
        let active_command_runs_cancelled = state.command_run.cancel_session(&session_id);
        let command_runs_drained = tokio::time::timeout(
            Duration::from_secs(10),
            state.command_run.wait_for_session_idle(&session_id),
        )
        .await
        .is_ok();
        if !command_runs_drained {
            return json!({
                "status": "error",
                "error": "TURA_SESSION_ACTIVE_COMMAND_CANCELLATION_DID_NOT_DRAIN",
                "session_id": session_id,
                "runtime_id": runtime_id,
                "stopped_worker": stopped_worker,
                "active_command_runs_cancelled": active_command_runs_cancelled,
                "active_command_runs_remaining": state.command_run.active_count_for_session(&session_id),
            });
        }
        let retained_process_scopes_terminated =
            code_tools::shell_executor::terminate_retained_shell_process_scopes_for_scope(
                &session_id,
            );
        self.retained_slots.lock().remove(&session_id);
        if let Some(notify) = self.retained_watchers.lock().remove(&session_id) {
            notify.notify_one();
        }
        let terminalization = self
            .terminalize_cancelled_runtime(state, &session_id, &runtime_id, &lease)
            .await;
        let terminalization_error = terminalization.as_ref().err().map(ToString::to_string);
        let runtime_terminalized = terminalization.is_ok();
        json!({
            "status": if runtime_terminalized { "cancelled" } else { "error" },
            "session_id": session_id,
            "runtime_id": runtime_id,
            "stopped_worker": stopped_worker,
            "active_command_runs_cancelled": active_command_runs_cancelled,
            "active_command_runs_remaining": state.command_run.active_count_for_session(&session_id),
            "retained_process_scopes_terminated": retained_process_scopes_terminated,
            "runtime_terminalized": runtime_terminalized,
            "terminalization_pending": !runtime_terminalized,
            "active_turn_removed": false,
            "terminalization_error": terminalization_error
        })
    }

    pub async fn kill_session_workers(&self, state: &AppState, input: Value) -> Value {
        let session_id = input
            .get("session_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string);
        let Some(session_id) = session_id else {
            let stopped = state
                .manager
                .stop_workers_with_prefix("runtime_worker:")
                .await;
            let leases = self.sessions.lock().clone();
            let mut terminalized_count = 0;
            let mut terminalization_failures = Vec::new();
            for (session_id, lease) in leases {
                state.command_run.cancel_session(&session_id);
                let drained = tokio::time::timeout(
                    Duration::from_secs(10),
                    state.command_run.wait_for_session_idle(&session_id),
                )
                .await
                .is_ok();
                code_tools::shell_executor::terminate_retained_shell_process_scopes_for_scope(
                    &session_id,
                );
                self.retained_slots.lock().remove(&session_id);
                if let Some(notify) = self.retained_watchers.lock().remove(&session_id) {
                    notify.notify_one();
                }
                let terminalization = if drained {
                    self.terminalize_cancelled_runtime(
                        state,
                        &session_id,
                        &lease.runtime_id,
                        &lease,
                    )
                    .await
                } else {
                    Err(anyhow!(
                        "RUNTIME_TERMINALIZATION_COMMAND_RUN_DID_NOT_DRAIN:session={session_id},runtime={}",
                        lease.runtime_id
                    ))
                };
                match terminalization {
                    Ok(()) => terminalized_count += 1,
                    Err(error) => terminalization_failures.push(json!({
                        "session_id": session_id,
                        "runtime_id": lease.runtime_id,
                        "error": error.to_string()
                    })),
                }
            }
            return json!({
                "status": if terminalization_failures.is_empty() { "stopped" } else { "error" },
                "stopped": stopped,
                "stopped_worker": stopped > 0,
                "active_turns_removed": 0,
                "terminalized_count": terminalized_count,
                "terminalizing_count": 0,
                "terminalization_failures": terminalization_failures
            });
        };

        let lease = self.sessions.lock().get(&session_id).cloned();
        let stopped_worker = state
            .manager
            .stop_worker_by_key(&format!("runtime_worker:{session_id}"))
            .await;
        let active_command_runs_cancelled = state.command_run.cancel_session(&session_id);
        let command_runs_drained = tokio::time::timeout(
            Duration::from_secs(10),
            state.command_run.wait_for_session_idle(&session_id),
        )
        .await
        .is_ok();
        let retained_process_scopes_terminated =
            code_tools::shell_executor::terminate_retained_shell_process_scopes_for_scope(
                &session_id,
            );
        self.retained_slots.lock().remove(&session_id);
        if let Some(notify) = self.retained_watchers.lock().remove(&session_id) {
            notify.notify_one();
        }
        let terminalization = match lease.as_ref() {
            Some(lease) if command_runs_drained => {
                self.terminalize_cancelled_runtime(state, &session_id, &lease.runtime_id, lease)
                    .await
            }
            Some(lease) => Err(anyhow!(
                "RUNTIME_TERMINALIZATION_COMMAND_RUN_DID_NOT_DRAIN:session={session_id},runtime={}",
                lease.runtime_id
            )),
            None => Ok(()),
        };
        let terminalization_error = terminalization.as_ref().err().map(ToString::to_string);
        let runtime_terminalized =
            lease.is_none() || (command_runs_drained && terminalization.is_ok());
        json!({
            "status": if runtime_terminalized { "stopped" } else { "error" },
            "session_id": session_id,
            "stopped": usize::from(stopped_worker),
            "stopped_worker": stopped_worker,
            "active_turn_removed": false,
            "active_command_runs_cancelled": active_command_runs_cancelled,
            "active_command_runs_remaining": state.command_run.active_count_for_session(&session_id),
            "retained_process_scopes_terminated": retained_process_scopes_terminated,
            "runtime_terminalized": runtime_terminalized,
            "terminalization_pending": false,
            "terminalization_error": terminalization_error
        })
    }

    pub async fn probe_sessions(&self, state: &AppState, input: Value) -> Result<Value> {
        let request: ProbeSessionsRequest = serde_json::from_value(input)?;
        let states = self.sessions.lock().clone();
        let mut sessions = Vec::new();
        for session_id in request
            .session_ids
            .into_iter()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
        {
            let lease = states.get(&session_id);
            let queued_turn =
                lease.is_some_and(|lease| !lease.slot_acquired && !lease.terminalizing);
            let running_turn =
                lease.is_some_and(|lease| lease.slot_acquired && !lease.terminalizing);
            let active_turn = lease.is_some_and(|lease| !lease.terminalizing);
            let worker_alive = state
                .manager
                .worker_alive_by_key(&format!("runtime_worker:{session_id}"))
                .await;
            let status = if lease.is_some_and(|lease| lease.terminalizing) {
                "terminalizing"
            } else if queued_turn {
                "queued"
            } else if running_turn || worker_alive {
                "running"
            } else {
                "inactive"
            };
            sessions.push(json!({
                "session_id": session_id,
                "runtime_id": lease.map(|lease| lease.runtime_id.clone()),
                "active_turn": active_turn,
                "queued_turn": queued_turn,
                "running_turn": running_turn,
                "worker_alive": worker_alive,
                "status": status
            }));
        }
        Ok(json!({ "sessions": sessions }))
    }

    pub async fn status(&self, state: &AppState) -> Value {
        let leases = self.sessions.lock().clone();
        let retained = self
            .retained_slots
            .lock()
            .keys()
            .cloned()
            .collect::<Vec<_>>();
        let mut sessions = Vec::with_capacity(leases.len());
        for (session_id, lease) in leases {
            let worker_alive = state
                .manager
                .worker_alive_by_key(&format!("runtime_worker:{session_id}"))
                .await;
            sessions.push(json!({
                "session_id": session_id,
                "runtime_id": lease.runtime_id,
                "transaction_id": lease.transaction_id,
                "slot_acquired": lease.slot_acquired,
                "terminalizing": lease.terminalizing,
                "worker_alive": worker_alive,
                "active_command_runs": state.command_run.active_count_for_session(&session_id),
                "retained_process_scopes": code_tools::shell_executor::retained_shell_process_scope_count_for_scope(&session_id),
                "retained_slot": retained.iter().any(|value| value == &session_id)
            }));
        }
        json!({
            "status": "ok",
            "active_session_count": sessions.len(),
            "retained_slot_count": retained.len(),
            "active_command_runs": state.command_run.active_count(),
            "sessions": sessions
        })
    }

    pub fn active_session_count(&self) -> usize {
        self.sessions.lock().len()
    }

    pub(crate) fn intake_terminal_feed_entry(
        &self,
        entry: &SessionFeedEntry,
        transaction_id: &str,
    ) -> Result<Option<TerminalDeliveryIdentity>> {
        let SessionFeedEvent::SessionProjectionUpdated { projection, .. } = &entry.event else {
            return Ok(None);
        };
        if !projection.state.is_terminal() {
            return Ok(None);
        }
        let Some(runtime_id) = entry
            .runtime_id
            .as_deref()
            .filter(|value| !value.trim().is_empty())
        else {
            // Session commands also publish terminal projections. They carry no
            // runtime identity and therefore cannot own a terminal receipt.
            return Ok(None);
        };
        let lease = self
            .sessions
            .lock()
            .get(&entry.session_id)
            .cloned()
            .ok_or_else(|| anyhow!("TERMINAL_FEED_LEASE_NOT_FOUND:{}", entry.session_id))?;
        if lease.transaction_id != transaction_id {
            return Err(anyhow!(
                "TERMINAL_FEED_IDENTITY_MISMATCH:session={},runtime={},transaction={}",
                entry.session_id,
                runtime_id,
                transaction_id
            ));
        }
        if !terminal_runtime_is_current(
            projection,
            &entry.session_id,
            &lease.runtime_id,
            runtime_id,
        )? {
            return Ok(None);
        }
        let snapshot = read_runtime_lease_snapshot(runtime_id)?;
        let expected_terminal_state = runtime_terminal_state_from_snapshot(&snapshot, projection)?;
        let store = lifecycle_store(&lease.commander_session_id)?;
        intake_terminal_receipt(
            &store,
            entry,
            runtime_id,
            transaction_id,
            &lease,
            expected_terminal_state,
        )
    }

    #[allow(
        dead_code,
        reason = "reserved for an explicit Commander consumer acknowledgement"
    )]
    pub(crate) fn acknowledge_terminal_delivery(
        &self,
        delivery: &TerminalDeliveryIdentity,
    ) -> Result<()> {
        let store = lifecycle_store(&delivery.commander_session_id)?;
        if let (Some(payload_sha256), Some(effect_identity)) = (
            delivery.callback_payload_sha256.as_deref(),
            delivery.callback_effect_identity.as_ref(),
        ) {
            store.acknowledge_callback(
                &delivery.transaction_id,
                &delivery.event_id,
                payload_sha256,
                effect_identity,
            )?;
        }
        store.acknowledge(
            &delivery.transaction_id,
            &delivery.event_id,
            &delivery.transaction_id,
        )?;
        Ok(())
    }

    pub(crate) async fn continue_terminal_delivery(
        &self,
        state: &AppState,
        delivery: &TerminalDeliveryIdentity,
    ) -> Result<Value> {
        let store = lifecycle_store(&delivery.commander_session_id)?;
        self.continue_terminal_delivery_with_store(state, delivery, &store)
            .await
    }

    async fn continue_terminal_delivery_with_store(
        &self,
        _state: &AppState,
        delivery: &TerminalDeliveryIdentity,
        store: &SessionLifecycleStore,
    ) -> Result<Value> {
        let persisted =
            store.callback_continuation(&delivery.transaction_id, &delivery.event_id)?;
        let (continuation, callback_to_intake) = if let Some(record) = persisted {
            (record, None)
        } else {
            let callback = store
                .callbacks_for_replay()?
                .into_iter()
                .find(|record| {
                    record.transaction_id == delivery.transaction_id
                        && record.event_id == delivery.event_id
                })
                .ok_or_else(|| {
                    anyhow!(
                        "PERSISTED_CALLBACK_FOR_CONTINUATION_NOT_FOUND:{}:{}",
                        delivery.transaction_id,
                        delivery.event_id
                    )
                })?;
            let continuation = ContinuationDispatchRecord::from_callback(&callback)?;
            (continuation, Some(callback))
        };
        if continuation.commander_session_id != delivery.commander_session_id
            || continuation.child_transaction_id != delivery.transaction_id
            || continuation.child_event_id != delivery.event_id
            || continuation.child_runtime_id != delivery.runtime_id
            || delivery.callback_payload_sha256.as_deref()
                != Some(continuation.callback_payload_sha256.as_str())
            || delivery.callback_effect_identity.as_ref() != Some(&continuation.effect_identity)
        {
            return Err(anyhow!(
                "CONTINUATION_DELIVERY_IDENTITY_MISMATCH:{}:{}",
                delivery.transaction_id,
                delivery.event_id
            ));
        }
        validate_direct_writer_continuation_chain(
            store,
            &continuation,
            callback_to_intake.as_ref(),
        )?;
        if let Some(callback) = callback_to_intake {
            store.mark_callback_intaken(
                &callback.transaction_id,
                &callback.event_id,
                &callback.callback_payload_sha256,
            )?;
        }
        if continuation.state == ContinuationDispatchState::DeliveryUnsettled {
            return reconcile_trusted_direct_thread_delivery(store, &continuation).await;
        }
        match store.prepare_callback_continuation(&continuation)? {
            ContinuationWriteOutcome::Acknowledged
            | ContinuationWriteOutcome::AlreadyAcknowledged => {
                return Ok(continuation_result(&continuation, "already_acknowledged"));
            }
            ContinuationWriteOutcome::AlreadyCompleted => {
                return Ok(continuation_result(
                    &continuation,
                    "completed_awaiting_commander_ack",
                ));
            }
            ContinuationWriteOutcome::AlreadyDispatched => {
                if store.callback_continuation_completion_proven(&continuation)? {
                    return Ok(continuation_result(
                        &continuation,
                        "direct_delivery_reconciled_awaiting_commander_ack",
                    ));
                }
                return Err(anyhow!(
                    "CONTINUATION_DIRECT_DELIVERY_RECONCILIATION_REQUIRED:{}:{}:{}",
                    continuation.request_id,
                    continuation.runtime_id,
                    continuation.lease_id
                ));
            }
            ContinuationWriteOutcome::Prepared | ContinuationWriteOutcome::AlreadyPrepared => {}
        }
        deliver_trusted_direct_thread_message(store, &continuation).await
    }

    pub(crate) async fn recover_callback_continuations(
        &self,
        state: &AppState,
        commander_session_id: &str,
    ) -> Result<Vec<Value>> {
        let Some(store) = lifecycle_store_if_exists(commander_session_id)? else {
            return Ok(Vec::new());
        };
        let callbacks = store.callbacks_for_replay()?;
        let continuations = store.callback_continuations_for_replay()?;
        let mut deliveries = Vec::new();
        for callback in callbacks {
            deliveries.push(TerminalDeliveryIdentity {
                commander_session_id: callback.commander_session_id.clone(),
                transaction_id: callback.transaction_id.clone(),
                event_id: callback.event_id.clone(),
                runtime_id: callback.runtime_id.clone(),
                callback_payload_sha256: Some(callback.callback_payload_sha256.clone()),
                callback_effect_identity: Some(callback.effect_identity.clone()),
            });
        }
        for continuation in continuations {
            if deliveries.iter().any(|delivery| {
                delivery.transaction_id == continuation.child_transaction_id
                    && delivery.event_id == continuation.child_event_id
            }) {
                continue;
            }
            deliveries.push(TerminalDeliveryIdentity {
                commander_session_id: continuation.commander_session_id.clone(),
                transaction_id: continuation.child_transaction_id.clone(),
                event_id: continuation.child_event_id.clone(),
                runtime_id: continuation.child_runtime_id.clone(),
                callback_payload_sha256: Some(continuation.callback_payload_sha256.clone()),
                callback_effect_identity: Some(continuation.effect_identity.clone()),
            });
        }
        let mut recovered = Vec::new();
        for delivery in deliveries {
            recovered.push(self.continue_terminal_delivery(state, &delivery).await?);
        }
        Ok(recovered)
    }

    pub(crate) fn publish_terminal_callback(
        &self,
        mut delivery: TerminalDeliveryIdentity,
        transport_payload: Value,
    ) -> Result<(Value, TerminalDeliveryIdentity)> {
        let store = lifecycle_store(&delivery.commander_session_id)?;
        Self::publish_terminal_callback_from_store(&store, &mut delivery, transport_payload)
    }

    fn publish_terminal_callback_from_store(
        store: &SessionLifecycleStore,
        delivery: &mut TerminalDeliveryIdentity,
        transport_payload: Value,
    ) -> Result<(Value, TerminalDeliveryIdentity)> {
        let receipt = store.terminal_receipt(&delivery.transaction_id, &delivery.event_id)?;
        if receipt.child_session_id == receipt.commander_session_id {
            return Ok((transport_payload, delivery.clone()));
        }
        let parent_mission_revision_sha256 = receipt
            .audit_metadata
            .get("parent_mission_revision_sha256")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                anyhow!("DELEGATED_LIFECYCLE_IDENTITY_MISSING:parent_mission_revision_sha256")
            })?;
        let delegated_input_sha256 = receipt
            .audit_metadata
            .get("delegated_input_sha256")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                anyhow!("DELEGATED_LIFECYCLE_IDENTITY_MISSING:delegated_input_sha256")
            })?;
        let callback_payload = transport_payload
            .pointer("/payload/body/item/text")
            .cloned()
            .ok_or_else(|| anyhow!("TERMINAL_CALLBACK_PAYLOAD_MISSING:{}", delivery.event_id))?;
        let effect_id = transport_payload
            .pointer("/payload/body/item/id")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| {
                anyhow!(
                    "TERMINAL_CALLBACK_EFFECT_IDENTITY_MISSING:{}",
                    delivery.event_id
                )
            })?
            .to_string();
        let admission = require_terminal_callback_admission(
            store,
            &receipt,
            parent_mission_revision_sha256,
            delegated_input_sha256,
            Some(&effect_id),
        )?;
        let mut record = DurableCallbackRecord::new(
            &receipt,
            callback_payload,
            transport_payload,
            parent_mission_revision_sha256,
            delegated_input_sha256,
            CallbackEffectIdentity::Exact { effect_id },
        )?;
        record.commander_thread_id = admission.commander_thread_id;
        record.callback_delivery_route = admission.callback_delivery_route;
        store.publish_callback(&record)?;
        store.mark_callback_intaken(
            &record.transaction_id,
            &record.event_id,
            &record.callback_payload_sha256,
        )?;
        delivery.callback_payload_sha256 = Some(record.callback_payload_sha256.clone());
        delivery.callback_effect_identity = Some(record.effect_identity.clone());
        Ok((record.transport_payload, delivery.clone()))
    }

    pub(crate) fn replay_terminal_callbacks(
        &self,
        commander_session_id: &str,
        child_session_id: &str,
        transaction_id: &str,
    ) -> Result<Vec<(Value, TerminalDeliveryIdentity)>> {
        let Some(store) = lifecycle_store_if_exists(commander_session_id)? else {
            return Ok(Vec::new());
        };
        replay_terminal_callbacks_from_store(
            &store,
            commander_session_id,
            child_session_id,
            transaction_id,
        )
    }

    pub(crate) fn publish_terminal_failure_callback(
        &self,
        delivery: TerminalDeliveryIdentity,
    ) -> Result<Option<(Value, TerminalDeliveryIdentity)>> {
        let store = lifecycle_store(&delivery.commander_session_id)?;
        publish_terminal_failure_callback_from_store(&store, delivery)
    }

    #[cfg(test)]
    pub(crate) fn set_session_lease_for_test(&self, session_id: &str, slot_acquired: bool) {
        self.sessions.lock().insert(
            session_id.to_string(),
            RuntimeLease {
                runtime_id: format!("runtime-{session_id}"),
                lease_id: format!("lease-{session_id}"),
                commander_session_id: session_id.to_string(),
                transaction_id: format!("transaction-{session_id}"),
                parent_mission_revision_sha256: None,
                delegated_input_sha256: None,
                task_id: None,
                goal_id: None,
                operator_override: false,
                receipt_event_seq: 0,
                slot_acquired,
                terminalizing: false,
            },
        );
    }
    async fn acquire_runtime_slot(
        &self,
        session_id: &str,
        maximum_parallel_runtime_workers: usize,
    ) -> Result<RuntimeSlotPermit> {
        if debug_runtime_enabled() {
            eprintln!(
                "router debug: enqueue_turn waiting for runtime slot session_id={session_id} limit={maximum_parallel_runtime_workers}"
            );
        }
        let permit = self
            .runtime_slots
            .acquire(maximum_parallel_runtime_workers)
            .await;
        if debug_runtime_enabled() {
            eprintln!("router debug: enqueue_turn acquired runtime slot session_id={session_id}");
        }
        Ok(permit)
    }

    fn mark_slot_acquired(&self, session_id: &str, runtime_id: &str) -> bool {
        let mut sessions = self.sessions.lock();
        let Some(lease) = sessions.get_mut(session_id) else {
            return false;
        };
        if lease.runtime_id != runtime_id {
            return false;
        }
        lease.slot_acquired = true;
        true
    }

    fn mark_terminalizing(&self, session_id: &str, runtime_id: &str) -> Result<RuntimeLease> {
        let mut sessions = self.sessions.lock();
        let lease = sessions
            .get_mut(session_id)
            .ok_or_else(|| anyhow!("RUNTIME_TERMINALIZATION_LEASE_NOT_FOUND:{session_id}"))?;
        if lease.runtime_id != runtime_id {
            return Err(anyhow!(
                "RUNTIME_TERMINALIZATION_IDENTITY_MISMATCH:session={session_id},expected_runtime={runtime_id},current_runtime={}",
                lease.runtime_id
            ));
        }
        lease.terminalizing = true;
        Ok(lease.clone())
    }

    async fn terminalization_quiescence_proof(
        &self,
        state: &AppState,
        session_id: &str,
        runtime_id: &str,
    ) -> Result<RuntimeRecoveryQuiescenceProof> {
        let terminalizing = self
            .sessions
            .lock()
            .get(session_id)
            .is_some_and(|lease| lease.runtime_id == runtime_id && lease.terminalizing);
        if !terminalizing {
            return Err(anyhow!(
                "RUNTIME_TERMINALIZATION_PHASE_NOT_OWNED:session={session_id},runtime={runtime_id}"
            ));
        }
        let worker_alive = state
            .manager
            .worker_alive_by_key(&format!("runtime_worker:{session_id}"))
            .await;
        let retained_process_scopes =
            code_tools::shell_executor::retained_shell_process_scope_count_for_scope(session_id);
        let retained_slot = self.retained_slots.lock().contains_key(session_id);
        let global_active_session_count = self.sessions.lock().len() as u64;
        let global_retained_slot_count = self.retained_slots.lock().len() as u64;
        let global_active_command_runs = state.command_run.active_count() as u64;
        let proof = RuntimeRecoveryQuiescenceProof {
            active_turn: false,
            queued_turn: false,
            running_turn: false,
            worker_alive,
            active_command_runs: state.command_run.active_count_for_session(session_id) as u64,
            retained_process_scopes: retained_process_scopes as u64,
            retained_slot,
            global_active_session_count,
            global_retained_slot_count,
            global_active_command_runs,
        };
        if !proof.is_quiescent() {
            return Err(anyhow!(
                "RUNTIME_TERMINALIZATION_NOT_QUIESCENT:{}",
                serde_json::to_string(&proof)?
            ));
        }
        Ok(proof)
    }

    async fn terminalize_registered_runtime(
        &self,
        state: &AppState,
        session_id: &str,
        runtime_id: &str,
        convergence_recovery: Option<&CommanderConvergenceRecoveryEvidence>,
    ) -> Result<()> {
        let lease = self.mark_terminalizing(session_id, runtime_id)?;
        tokio::time::timeout(
            Duration::from_secs(10),
            state.command_run.wait_for_session_idle(session_id),
        )
        .await
        .map_err(|_| {
            anyhow!(
                "RUNTIME_TERMINALIZATION_COMMAND_RUN_DID_NOT_DRAIN:session={session_id},runtime={runtime_id}"
            )
        })?;
        let proof = self
            .terminalization_quiescence_proof(state, session_id, runtime_id)
            .await?;
        state.session_db.start()?;
        let snapshot = match session_log_contract::client::call_service(
            &SessionLogCommand::GetRuntimeLease(GetRuntimeLeaseRequest {
                runtime_id: runtime_id.to_string(),
                database_path: None,
            }),
        )? {
            SessionLogResponse::RuntimeLeaseRead {
                runtime: Some(runtime),
            } => runtime,
            SessionLogResponse::RuntimeLeaseRead { runtime: None } => {
                return Err(anyhow!(
                    "RUNTIME_TERMINALIZATION_DURABLE_LEASE_NOT_FOUND:{runtime_id}"
                ));
            }
            SessionLogResponse::Error { error } => return Err(anyhow!(error)),
            other => {
                return Err(anyhow!(
                    "RUNTIME_TERMINALIZATION_LEASE_READ_UNEXPECTED:{other:?}"
                ));
            }
        };
        validate_terminalization_identity(&snapshot, &lease, session_id, runtime_id)?;
        if snapshot.terminal {
            if snapshot.lease_active {
                return Err(anyhow!(
                    "RUNTIME_TERMINALIZATION_DURABLE_STATE_CONFLICT:runtime={runtime_id},terminal=true,lease_active=true"
                ));
            }
            return Ok(());
        }
        if !snapshot.lease_active {
            return Err(anyhow!(
                "RUNTIME_TERMINALIZATION_DURABLE_STATE_CONFLICT:runtime={runtime_id},terminal=false,lease_active=false"
            ));
        }
        let reason = if convergence_recovery.is_some() {
            RecoveryCloseRuntimeReason::CommanderConvergenceProven
        } else if snapshot.revision == 0 && snapshot.last_event_seq == 0 {
            RecoveryCloseRuntimeReason::UnbornRuntime
        } else {
            RecoveryCloseRuntimeReason::OrphanedRuntime
        };
        let recovery = RecoveryCloseRuntimeRequest {
            receipt_id: format!(
                "router-auto-terminalize:{}:{}",
                lease.transaction_id, runtime_id
            ),
            database_path: snapshot.database_path.clone(),
            runtime_id: runtime_id.to_string(),
            session_id: session_id.to_string(),
            lease_id: snapshot.lease_id.clone(),
            expected_lease_active: snapshot.lease_active,
            expected_revision: snapshot.revision,
            expected_last_event_seq: snapshot.last_event_seq,
            expected_session_event_seq: snapshot.session_event_seq,
            expected_session_state: snapshot.session_state,
            reason,
            convergence_proof_sha256: convergence_recovery
                .map(|evidence| evidence.proof_sha256.clone()),
            quiescence: proof,
        };
        let result = match session_log_contract::client::call_service(
            &SessionLogCommand::RecoveryCloseRuntime(recovery),
        )? {
            SessionLogResponse::RuntimeRecoveryClosed { result } => result,
            SessionLogResponse::Error { error } => return Err(anyhow!(error)),
            other => {
                return Err(anyhow!(
                    "RUNTIME_TERMINALIZATION_RECOVERY_UNEXPECTED:{other:?}"
                ));
            }
        };
        match result {
            RecoveryCloseRuntimeOutcome::Closed { receipt }
            | RecoveryCloseRuntimeOutcome::AlreadyClosed { receipt }
                if receipt.runtime_id == runtime_id
                    && receipt.session_id == session_id
                    && receipt.lease_id == snapshot.lease_id
                    && receipt.terminal
                    && !receipt.lease_active =>
            {
                self.write_recovery_terminal_receipt(&lease, &receipt)?;
                Ok(())
            }
            other => Err(anyhow!(
                "RUNTIME_TERMINALIZATION_RECOVERY_REJECTED:{other:?}"
            )),
        }
    }

    async fn terminalize_cancelled_runtime(
        &self,
        state: &AppState,
        session_id: &str,
        runtime_id: &str,
        lease: &RuntimeLease,
    ) -> Result<()> {
        let terminalization = self
            .terminalize_registered_runtime(state, session_id, runtime_id, None)
            .await;
        if let Err(error) = terminalization {
            if !error
                .to_string()
                .starts_with("RUNTIME_TERMINALIZATION_LEASE_NOT_FOUND:")
            {
                return Err(error);
            }
            self.confirm_runtime_durably_closed(state, session_id, runtime_id, lease)
                .map_err(|readback_error| {
                    anyhow!(
                        "RUNTIME_CANCEL_TERMINALIZATION_FAILED:{error:#}:DURABLE_READBACK_FAILED:{readback_error:#}"
                    )
                })?;
        }
        Ok(())
    }

    fn write_recovery_terminal_receipt(
        &self,
        lease: &RuntimeLease,
        recovery: &RuntimeRecoveryReceipt,
    ) -> Result<()> {
        let receipt_lease_id = recovery.lease_id.as_deref().ok_or_else(|| {
            anyhow!(
                "RUNTIME_RECOVERY_TERMINAL_RECEIPT_LEASE_MISSING:{}",
                recovery.runtime_id
            )
        })?;
        let event_id = recovery_terminal_projection_event_id(&recovery.receipt_id);
        let mut receipt = TerminalReceipt::new(
            TerminalReceiptIdentity::new(
                &lease.transaction_id,
                event_id,
                lease.receipt_event_seq,
                &lease.commander_session_id,
                &recovery.session_id,
                &recovery.runtime_id,
                receipt_lease_id,
            ),
            recovery_terminal_state(recovery),
            recovery.closed_at,
        );
        receipt.task_id.clone_from(&lease.task_id);
        receipt.goal_id.clone_from(&lease.goal_id);
        receipt.operator_override = lease.operator_override;
        receipt.audit_metadata.insert(
            "runtime_event_seq".to_string(),
            json!(recovery.last_event_seq),
        );
        receipt.audit_metadata.insert(
            "runtime_expected_revision".to_string(),
            json!(recovery.revision.saturating_sub(1)),
        );
        receipt.audit_metadata.insert(
            "runtime_state".to_string(),
            recovery_runtime_state(recovery),
        );
        receipt
            .audit_metadata
            .insert("session_state".to_string(), json!(recovery.session_state));
        receipt
            .audit_metadata
            .insert("dispatch_runtime_id".to_string(), json!(lease.runtime_id));
        receipt
            .audit_metadata
            .insert("dispatch_lease_id".to_string(), json!(lease.lease_id));
        if let Some(value) = &lease.parent_mission_revision_sha256 {
            receipt
                .audit_metadata
                .insert("parent_mission_revision_sha256".to_string(), json!(value));
        }
        if let Some(value) = &lease.delegated_input_sha256 {
            receipt
                .audit_metadata
                .insert("delegated_input_sha256".to_string(), json!(value));
        }
        lifecycle_store(&lease.commander_session_id)?
            .write_terminal_receipt(&receipt)
            .map_err(|error| anyhow!(error.to_string()))?;
        Ok(())
    }

    fn write_snapshot_terminal_receipt(
        &self,
        lease: &RuntimeLease,
        snapshot: &RuntimeLeaseSnapshot,
        entry: &SessionFeedEntry,
        terminal_state: TerminalState,
    ) -> Result<()> {
        let store = lifecycle_store(&lease.commander_session_id)?;
        Self::ensure_snapshot_terminal_receipt(&store, lease, snapshot, entry, terminal_state)
    }

    fn ensure_snapshot_terminal_receipt(
        store: &SessionLifecycleStore,
        lease: &RuntimeLease,
        snapshot: &RuntimeLeaseSnapshot,
        entry: &SessionFeedEntry,
        terminal_state: TerminalState,
    ) -> Result<()> {
        match store.terminal_receipt(&lease.transaction_id, &entry.event_id) {
            Ok(_) => return Ok(()),
            Err(error) if error.code == "TERMINAL_RECEIPT_NOT_FOUND" => {}
            Err(error) => return Err(anyhow!(error.to_string())),
        }
        let receipt = Self::snapshot_terminal_receipt(lease, snapshot, entry, terminal_state)?;
        store
            .write_terminal_receipt(&receipt)
            .map_err(|error| anyhow!(error.to_string()))?;
        Ok(())
    }

    fn snapshot_terminal_receipt(
        lease: &RuntimeLease,
        snapshot: &RuntimeLeaseSnapshot,
        entry: &SessionFeedEntry,
        terminal_state: TerminalState,
    ) -> Result<TerminalReceipt> {
        let receipt_lease_id = snapshot.lease_id.as_deref().ok_or_else(|| {
            anyhow!(
                "RUNTIME_SNAPSHOT_TERMINAL_RECEIPT_LEASE_MISSING:{}",
                snapshot.runtime_id
            )
        })?;
        let finished_at_ms = match &entry.event {
            SessionFeedEvent::SessionProjectionUpdated { updated_at, .. } => *updated_at,
            _ => {
                return Err(anyhow!(
                    "RUNTIME_CALLBACK_TERMINAL_PROJECTION_MISSING:{}",
                    snapshot.runtime_id
                ));
            }
        };
        let mut receipt = TerminalReceipt::new(
            TerminalReceiptIdentity::new(
                &lease.transaction_id,
                &entry.event_id,
                lease.receipt_event_seq,
                &lease.commander_session_id,
                &snapshot.session_id,
                &snapshot.runtime_id,
                receipt_lease_id,
            ),
            terminal_state,
            finished_at_ms,
        );
        receipt.task_id.clone_from(&lease.task_id);
        receipt.goal_id.clone_from(&lease.goal_id);
        receipt.operator_override = lease.operator_override;
        receipt.audit_metadata.insert(
            "runtime_event_seq".to_string(),
            json!(snapshot.last_event_seq),
        );
        receipt.audit_metadata.insert(
            "runtime_expected_revision".to_string(),
            json!(snapshot.revision.saturating_sub(1)),
        );
        receipt.audit_metadata.insert(
            "runtime_state".to_string(),
            snapshot
                .runtime_state
                .map_or(Value::Null, |state| json!(state)),
        );
        receipt
            .audit_metadata
            .insert("dispatch_runtime_id".to_string(), json!(lease.runtime_id));
        receipt
            .audit_metadata
            .insert("dispatch_lease_id".to_string(), json!(lease.lease_id));
        if let Some(value) = &lease.parent_mission_revision_sha256 {
            receipt
                .audit_metadata
                .insert("parent_mission_revision_sha256".to_string(), json!(value));
        }
        if let Some(value) = &lease.delegated_input_sha256 {
            receipt
                .audit_metadata
                .insert("delegated_input_sha256".to_string(), json!(value));
        }
        Ok(receipt)
    }

    fn confirm_runtime_durably_closed(
        &self,
        state: &AppState,
        session_id: &str,
        runtime_id: &str,
        lease: &RuntimeLease,
    ) -> Result<()> {
        state.session_db.start()?;
        let snapshot = match session_log_contract::client::call_service(
            &SessionLogCommand::GetRuntimeLease(GetRuntimeLeaseRequest {
                runtime_id: runtime_id.to_string(),
                database_path: None,
            }),
        )? {
            SessionLogResponse::RuntimeLeaseRead {
                runtime: Some(runtime),
            } => runtime,
            SessionLogResponse::RuntimeLeaseRead { runtime: None } => {
                return Err(anyhow!(
                    "RUNTIME_CANCEL_DURABLE_LEASE_NOT_FOUND:{runtime_id}"
                ));
            }
            SessionLogResponse::Error { error } => return Err(anyhow!(error)),
            other => {
                return Err(anyhow!("RUNTIME_CANCEL_LEASE_READ_UNEXPECTED:{other:?}"));
            }
        };
        validate_terminalization_identity(&snapshot, lease, session_id, runtime_id)?;
        if !snapshot.terminal || snapshot.lease_active {
            return Err(anyhow!(
                "RUNTIME_CANCEL_DURABLE_STATE_CONFLICT:runtime={runtime_id},terminal={},lease_active={}",
                snapshot.terminal,
                snapshot.lease_active
            ));
        }
        Ok(())
    }

    fn ensure_terminal_receipt(
        &self,
        session_id: &str,
        dispatch_runtime_id: &str,
        transaction_id: &str,
    ) -> Result<TerminalDeliveryIdentity> {
        let mut after_cursor = 0;
        let mut latest = None;
        loop {
            let response = session_log_contract::client::call_service(
                &SessionLogCommand::ReadSessionFeed(session_log_contract::ReadSessionFeedRequest {
                    session_id: session_id.to_string(),
                    after_cursor,
                    limit: 1_000,
                }),
            )?;
            let SessionLogResponse::SessionFeed {
                entries,
                next_cursor,
            } = response
            else {
                return Err(anyhow!("TERMINAL_FEED_READ_FAILED:{response:?}"));
            };
            for entry in &entries {
                if let Some(delivery) = self.intake_terminal_feed_entry(entry, transaction_id)? {
                    latest = Some(delivery);
                }
            }
            if next_cursor <= after_cursor {
                return latest.ok_or_else(|| {
                    anyhow!(
                        "TERMINAL_FEED_EVENT_NOT_FOUND:session={session_id},runtime={dispatch_runtime_id}"
                    )
                });
            }
            after_cursor = next_cursor;
        }
    }

    fn retain_runtime_slot_if_current(
        &self,
        session_id: &str,
        runtime_id: &str,
        permit: RuntimeSlotPermit,
    ) -> std::result::Result<(), RuntimeSlotPermit> {
        let sessions = self.sessions.lock();
        if !sessions
            .get(session_id)
            .is_some_and(|lease| lease.runtime_id == runtime_id)
        {
            return Err(permit);
        }
        self.retained_slots
            .lock()
            .insert(session_id.to_string(), permit);
        Ok(())
    }

    async fn live_effect_evidence(&self, state: &AppState, session_id: &str) -> LiveEffectEvidence {
        LiveEffectEvidence {
            runtime_worker_alive: state
                .manager
                .worker_alive_by_key(&format!("runtime_worker:{session_id}"))
                .await,
            active_tool_calls: 0,
            active_command_runs: state.command_run.active_count_for_session(session_id),
            live_effect_processes:
                code_tools::shell_executor::retained_shell_process_scope_count_for_scope(session_id),
            pending_init: false,
        }
    }

    fn spawn_retained_reclaimer(
        &self,
        state: AppState,
        session_id: String,
        delivery: TerminalDeliveryIdentity,
    ) {
        let notify = Arc::new(Notify::new());
        if self
            .retained_watchers
            .lock()
            .insert(session_id.clone(), Arc::clone(&notify))
            .is_some()
        {
            return;
        }
        let service = self.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_millis(250)).await;
                if !service.retained_slots.lock().contains_key(&session_id) {
                    if let Some(notify) = service.retained_watchers.lock().remove(&session_id) {
                        notify.notify_one();
                    }
                    return;
                }
                let evidence = service.live_effect_evidence(&state, &session_id).await;
                let outcome = match lifecycle_store(&delivery.commander_session_id).and_then(
                    |store| {
                        store
                            .reclaim_terminal_slot(
                                &delivery.transaction_id,
                                &delivery.event_id,
                                evidence,
                            )
                            .map_err(|error| anyhow!(error.to_string()))
                    },
                ) {
                    Ok(outcome) => outcome,
                    Err(error) => {
                        eprintln!(
                            "router retained execution reclaimer deferred session={session_id}: {error:#}"
                        );
                        continue;
                    }
                };
                match outcome {
                    ReclaimOutcome::Released | ReclaimOutcome::AlreadyReleased => {
                        service.retained_slots.lock().remove(&session_id);
                        service.retained_watchers.lock().remove(&session_id);
                        notify.notify_one();
                        return;
                    }
                    ReclaimOutcome::Retained { .. } => {}
                }
            }
        });
    }

    async fn wait_for_retained_release(&self, session_id: &str) {
        loop {
            let Some(notify) = self.retained_watchers.lock().get(session_id).cloned() else {
                return;
            };
            let notified = notify.notified();
            if !self.retained_slots.lock().contains_key(session_id) {
                return;
            }
            notified.await;
        }
    }
}

fn continuation_result(record: &ContinuationDispatchRecord, status: &str) -> Value {
    json!({
        "status": status,
        "request_id": record.request_id,
        "runtime_id": record.runtime_id,
        "lease_id": record.lease_id,
        "session_id": record.commander_session_id,
    })
}

fn trusted_direct_thread_message(
    record: &ContinuationDispatchRecord,
) -> Result<(String, String, String)> {
    let target_thread_id = record.commander_thread_id.as_deref().ok_or_else(|| {
        anyhow!(
            "CONTINUATION_COMMANDER_THREAD_ID_MISSING:{}",
            record.request_id
        )
    })?;
    let payload_sha256 = canonical_value_sha256(&record.parent_input);
    let delivery_marker = format!(
        "tura-direct-callback:{}:{}",
        record.request_id, payload_sha256
    );
    let message = serde_json::to_string(&json!({
        "schema_version": "tura_trusted_direct_thread_injection_v1",
        "delivery_marker": &delivery_marker,
        "continuation_request_id": &record.request_id,
        "target_thread_id": target_thread_id,
        "requested_action": &record.requested_action,
        "callback_payload_sha256": &record.callback_payload_sha256,
        "parent_mission_revision_sha256": &record.parent_mission_revision_sha256,
        "payload_sha256": payload_sha256,
        "payload": &record.parent_input,
    }))?;
    let message_sha256 = format!("{:x}", Sha256::digest(message.as_bytes()));
    Ok((message, message_sha256, delivery_marker))
}

fn value_contains_delivery_marker(value: &Value, marker: &str) -> bool {
    match value {
        Value::String(text) => text.contains(marker),
        Value::Array(values) => values
            .iter()
            .any(|value| value_contains_delivery_marker(value, marker)),
        Value::Object(values) => values
            .values()
            .any(|value| value_contains_delivery_marker(value, marker)),
        Value::Null | Value::Bool(_) | Value::Number(_) => false,
    }
}

enum DirectDeliveryAdmission {
    Send,
    Reconcile(ContinuationDispatchRecord),
    Settled(&'static str),
}

fn admit_trusted_direct_thread_delivery(
    store: &SessionLifecycleStore,
    record: &ContinuationDispatchRecord,
    message: &str,
) -> Result<DirectDeliveryAdmission> {
    match store.begin_callback_continuation_direct_delivery(record, message)? {
        ContinuationWriteOutcome::Prepared => Ok(DirectDeliveryAdmission::Send),
        ContinuationWriteOutcome::AlreadyDispatched => {
            let current = store
                .callback_continuation(&record.child_transaction_id, &record.child_event_id)?
                .ok_or_else(|| {
                    anyhow!(
                        "CONTINUATION_DIRECT_DELIVERY_RECORD_MISSING:{}",
                        record.request_id
                    )
                })?;
            if store.callback_continuation_completion_proven(&current)? {
                Ok(DirectDeliveryAdmission::Settled(
                    "direct_delivery_reconciled_awaiting_commander_ack",
                ))
            } else {
                Ok(DirectDeliveryAdmission::Reconcile(current))
            }
        }
        ContinuationWriteOutcome::AlreadyCompleted => Ok(DirectDeliveryAdmission::Settled(
            "completed_awaiting_commander_ack",
        )),
        ContinuationWriteOutcome::Acknowledged | ContinuationWriteOutcome::AlreadyAcknowledged => {
            Ok(DirectDeliveryAdmission::Settled("already_acknowledged"))
        }
        ContinuationWriteOutcome::AlreadyPrepared => Err(anyhow!(
            "CONTINUATION_DIRECT_DELIVERY_ALREADY_IN_PROGRESS_NO_BLIND_RETRY:{}",
            record.request_id
        )),
    }
}

#[cfg(unix)]
async fn deliver_trusted_direct_thread_message(
    store: &SessionLifecycleStore,
    record: &ContinuationDispatchRecord,
) -> Result<Value> {
    let target_thread_id = record.commander_thread_id.as_deref().ok_or_else(|| {
        anyhow!(
            "CONTINUATION_COMMANDER_THREAD_ID_MISSING:{}",
            record.request_id
        )
    })?;
    let (message, message_sha256, delivery_marker) = trusted_direct_thread_message(record)?;
    let client = DirectThreadWriterClient::from_environment()?;
    client.preflight(&record.request_id).await?;
    match admit_trusted_direct_thread_delivery(store, record, &message)? {
        DirectDeliveryAdmission::Send => {}
        DirectDeliveryAdmission::Reconcile(current) => {
            return reconcile_trusted_direct_thread_delivery(store, &current).await;
        }
        DirectDeliveryAdmission::Settled(status) => {
            return Ok(continuation_result(record, status));
        }
    }
    let send = client
        .send_message_to_thread(
            &record.request_id,
            target_thread_id,
            &record.request_id,
            &message,
        )
        .await?;
    let acceptance = json!({
        "schema_version": "tura_direct_thread_delivery_acceptance_v1",
        "request_id": &record.request_id,
        "target_thread_id": target_thread_id,
        "message_sha256": &message_sha256,
        "delivery_marker": &delivery_marker,
        "send_endpoint_pid": send.endpoint_pid,
        "send_result": &send.result,
    });
    store.mark_callback_continuation_delivery_accepted(record, &acceptance)?;
    let read = client
        .read_thread(
            &format!("reconcile-{}", record.request_id),
            target_thread_id,
            &format!("reconcile-turn-{}", record.request_id),
            &format!("reconcile-call-{}", record.request_id),
        )
        .await?;
    if !value_contains_delivery_marker(&read.result, &delivery_marker) {
        return Err(anyhow!(
            "DIRECT_THREAD_WRITER_READBACK_NOT_OBSERVED:{}:{}",
            record.request_id,
            message_sha256
        ));
    }
    let evidence = json!({
        "schema_version": "tura_direct_thread_delivery_reconciliation_v1",
        "request_id": &record.request_id,
        "target_thread_id": target_thread_id,
        "message_sha256": message_sha256,
        "delivery_marker": delivery_marker,
        "send_endpoint_pid": send.endpoint_pid,
        "send_result": send.result,
        "read_endpoint_pid": read.endpoint_pid,
        "read_result": read.result,
    });
    store.mark_callback_continuation_delivery_reconciled(record, &evidence)?;
    Ok(continuation_result(
        record,
        "direct_injection_reconciled_awaiting_commander_ack",
    ))
}

#[cfg(unix)]
async fn reconcile_trusted_direct_thread_delivery(
    store: &SessionLifecycleStore,
    record: &ContinuationDispatchRecord,
) -> Result<Value> {
    let target_thread_id = record.commander_thread_id.as_deref().ok_or_else(|| {
        anyhow!(
            "CONTINUATION_COMMANDER_THREAD_ID_MISSING:{}",
            record.request_id
        )
    })?;
    let (_message, message_sha256, delivery_marker) = trusted_direct_thread_message(record)?;
    if record.delivered_message_sha256.as_deref() != Some(message_sha256.as_str()) {
        return Err(anyhow!(
            "CONTINUATION_DIRECT_DELIVERY_MESSAGE_IDENTITY_MISMATCH:{}",
            record.request_id
        ));
    }
    let client = DirectThreadWriterClient::from_environment()?;
    let read = client
        .read_thread(
            &format!("reconcile-{}", record.request_id),
            target_thread_id,
            &format!("reconcile-turn-{}", record.request_id),
            &format!("reconcile-call-{}", record.request_id),
        )
        .await?;
    if !value_contains_delivery_marker(&read.result, &delivery_marker) {
        return Err(anyhow!(
            "CONTINUATION_DELIVERY_UNSETTLED_NO_BLIND_RETRY:{}",
            record.request_id
        ));
    }
    let evidence = json!({
        "schema_version": "tura_direct_thread_delivery_reconciliation_v1",
        "request_id": &record.request_id,
        "target_thread_id": target_thread_id,
        "message_sha256": message_sha256,
        "delivery_marker": delivery_marker,
        "recovered_after_uncertain_delivery": true,
        "read_endpoint_pid": read.endpoint_pid,
        "read_result": read.result,
    });
    store.mark_callback_continuation_delivery_reconciled(record, &evidence)?;
    Ok(continuation_result(
        record,
        "uncertain_delivery_reconciled_awaiting_commander_ack",
    ))
}

#[cfg(not(unix))]
async fn deliver_trusted_direct_thread_message(
    _store: &SessionLifecycleStore,
    record: &ContinuationDispatchRecord,
) -> Result<Value> {
    Err(anyhow!(
        "DIRECT_THREAD_WRITER_UNSUPPORTED_PLATFORM:{}",
        record.request_id
    ))
}

#[cfg(not(unix))]
async fn reconcile_trusted_direct_thread_delivery(
    _store: &SessionLifecycleStore,
    record: &ContinuationDispatchRecord,
) -> Result<Value> {
    Err(anyhow!(
        "DIRECT_THREAD_WRITER_UNSUPPORTED_PLATFORM:{}",
        record.request_id
    ))
}

fn commander_continuation_binding(
    record: &ContinuationDispatchRecord,
) -> Result<CommanderContinuationBinding> {
    let target_thread_id = record.commander_thread_id.clone().ok_or_else(|| {
        anyhow!(
            "COMMANDER_CONTINUATION_TARGET_MISSING:{}",
            record.request_id
        )
    })?;
    let effect = serde_json::to_value(&record.effect_identity)?;
    let binding = CommanderContinuationBinding {
        target_thread_id,
        requested_action: record.requested_action.clone(),
        continuation_request_id: record.request_id.clone(),
        child_session_id: record.child_session_id.clone(),
        child_transaction_id: record.child_transaction_id.clone(),
        child_runtime_id: record.child_runtime_id.clone(),
        callback_payload_sha256: record.callback_payload_sha256.clone(),
        effect_identity_sha256: canonical_value_sha256(&effect),
        pre_revision_sha256: record.parent_mission_revision_sha256.clone(),
    };
    binding.validate().map_err(anyhow::Error::msg)?;
    Ok(binding)
}

fn terminal_commander_convergence_recovery_evidence(
    session_directory: &std::path::Path,
    session_id: &str,
    runtime_id: &str,
    binding: &CommanderContinuationBinding,
) -> Result<Option<CommanderConvergenceRecoveryEvidence>> {
    let Some(ledger) = load_terminal_commander_convergence_ledger(
        session_directory,
        session_id,
        runtime_id,
        binding,
    )
    .map_err(anyhow::Error::msg)?
    else {
        return Ok(None);
    };
    let proof = ledger
        .commander_convergence_proof
        .as_ref()
        .ok_or_else(|| anyhow!("COMMANDER_CONVERGENCE_LEDGER_PROOF_MISSING:{runtime_id}"))?;
    Ok(Some(CommanderConvergenceRecoveryEvidence {
        proof_sha256: canonical_value_sha256(&serde_json::to_value(proof)?),
    }))
}

#[cfg(test)]
fn pre_provider_zero_effect_failure_evidence(runtime: &RuntimeAggregate) -> Option<String> {
    pre_provider_zero_effect_evidence(runtime, "PROVIDER_ROUTE_ADMISSION_REJECTED")
}

#[cfg(test)]
fn pre_provider_commander_active_writer_evidence(runtime: &RuntimeAggregate) -> Option<String> {
    let error_text = runtime.error.as_ref()?.error_text.as_deref()?;
    if runtime.provider.llm_provider_name != "official_codex_app_server"
        || !error_text.starts_with("official Codex App Server returned an error for thread/resume:")
        || !error_text.contains("\"code\":-32600")
        || !error_text.contains("already has an active writer")
    {
        return None;
    }
    pre_provider_zero_effect_evidence(runtime, "OFFICIAL_CODEX_APP_SERVER_FAILED")
}

#[cfg(test)]
fn pre_provider_commander_binding_recovery_evidence(runtime: &RuntimeAggregate) -> Option<String> {
    let error_text = runtime.error.as_ref()?.error_text.as_deref()?;
    if runtime.provider.llm_provider_name != "official_codex_app_server"
        || error_text != "COMMANDER_CONTINUATION_BINDING_INVALID: runtime/request identity mismatch"
    {
        return None;
    }
    pre_provider_zero_effect_evidence(runtime, "OFFICIAL_CODEX_APP_SERVER_FAILED")
}

#[cfg(test)]
fn pre_provider_commander_ledger_chain_evidence(runtime: &RuntimeAggregate) -> Option<String> {
    let error_text = runtime.error.as_ref()?.error_text.as_deref()?;
    if runtime.provider.llm_provider_name != "official_codex_app_server"
        || !error_text.starts_with(
            "OFFICIAL_CODEX_INTERRUPTED_RECOVERY_UNCERTAIN_EFFECT: effect 0 is not durably reconciled:",
        )
        || !error_text.ends_with("execution ledger fallback source is not durable")
    {
        return None;
    }
    pre_provider_zero_effect_evidence(runtime, "OFFICIAL_CODEX_APP_SERVER_FAILED")
}

#[cfg(test)]
fn pre_provider_zero_effect_evidence(
    runtime: &RuntimeAggregate,
    accepted_error_code: &str,
) -> Option<String> {
    let error = runtime.error.as_ref()?;
    let error_text = error.error_text.as_deref()?;
    let output = runtime.output.as_ref()?.as_object()?;
    if runtime.state != RuntimeState::Failed
        || runtime.called_at.is_none()
        || runtime.call_finished_at.is_none()
        || runtime.first_token_at.is_some()
        || runtime.usage.is_some()
        || runtime.context_tokens.input != 0
        || runtime.reasoning.is_some()
        || runtime.reasoning_hash.is_some()
        || !runtime.text.is_empty()
        || !runtime.tool_call.is_empty()
        || error.error_code.as_deref() != Some(accepted_error_code)
        || error.retry_allowed
        || error.fallback_allowed
        || error.fallback_to_id.is_some()
        || output.len() != 1
        || output.get("error").and_then(Value::as_str) != Some(error_text)
    {
        return None;
    }
    Some(canonical_value_sha256(&json!({
        "runtime_id": runtime.runtime_id,
        "session_id": runtime.session_id,
        "state": runtime.state,
        "called_at": runtime.called_at,
        "call_finished_at": runtime.call_finished_at,
        "error": error,
        "output": runtime.output,
        "context_tokens": runtime.context_tokens,
        "usage": runtime.usage,
        "first_token_at": runtime.first_token_at,
        "reasoning": runtime.reasoning,
        "reasoning_hash": runtime.reasoning_hash,
        "text": runtime.text,
        "tool_call": runtime.tool_call,
    })))
}

#[cfg(test)]
fn callback_continuation_payload(
    record: &ContinuationDispatchRecord,
    parent: &SessionMetadata,
    commander_continuation: Option<&CommanderContinuationBinding>,
) -> Result<Value> {
    Ok(json!({
        "prompt": serde_json::to_string(&record.parent_input)?,
        "directory": parent.session_directory,
        "model": parent.model,
        "agent": parent.agent,
        "session_type": parent.session_type,
        "parent_mission_revision_sha256": record.parent_mission_revision_sha256,
        "delegated_input_sha256": record.delegated_input_sha256,
        "lifecycle": {
            "transaction_id": record.request_id,
            "commander_session_id": record.commander_session_id,
            "parent_mission_revision_sha256": record.parent_mission_revision_sha256,
            "delegated_input_sha256": record.delegated_input_sha256,
            "task_id": null,
            "goal_id": null,
            "operator_override": false,
            "commander_continuation": commander_continuation,
        },
        "operator_override": false,
    }))
}

fn require_successful_runtime_dispatch(status: u16, body: &Value) -> Result<()> {
    if status < 400 {
        return Ok(());
    }
    Err(anyhow!(
        "{}",
        body.pointer("/result/error")
            .or_else(|| body.get("error"))
            .and_then(Value::as_str)
            .unwrap_or("runtime worker failed")
    ))
}

fn bind_commander_convergence_proof_from_runtime(
    store: &SessionLifecycleStore,
    record: &ContinuationDispatchRecord,
    completion_runtime_id: &str,
) -> Result<ContinuationDispatchRecord> {
    if record.commander_thread_id.is_none() {
        return Ok(record.clone());
    }
    let response = session_log_contract::client::call_service(&SessionLogCommand::ReplayRuntime(
        ReplayRuntimeRequest {
            runtime_id: completion_runtime_id.to_string(),
        },
    ))?;
    let runtime = match response {
        SessionLogResponse::RuntimeReplayed {
            runtime: Some(runtime),
        } => runtime.aggregate,
        SessionLogResponse::RuntimeReplayed { runtime: None } => {
            return Err(anyhow!(
                "COMMANDER_CONVERGENCE_RUNTIME_NOT_FOUND:{}",
                completion_runtime_id
            ));
        }
        SessionLogResponse::Error { error } => {
            return Err(anyhow!(
                "COMMANDER_CONVERGENCE_RUNTIME_REPLAY_FAILED:{}:{error}",
                completion_runtime_id
            ));
        }
        other => {
            return Err(anyhow!(
                "COMMANDER_CONVERGENCE_RUNTIME_REPLAY_UNEXPECTED:{}:{other:?}",
                completion_runtime_id
            ));
        }
    };
    let proof = commander_convergence_proof_from_runtime(record, &runtime)?;
    store.bind_callback_continuation_convergence_proof(record, &proof)?;
    let mut bound = record.clone();
    bound.convergence_proof = Some(proof);
    Ok(bound)
}

fn commander_convergence_proof_from_runtime(
    record: &ContinuationDispatchRecord,
    runtime: &RuntimeAggregate,
) -> Result<CommanderConvergenceProof> {
    let original_or_bound_fallback = runtime.runtime_id == record.runtime_id
        || runtime.fallback_from_id.as_deref() == Some(record.runtime_id.as_str())
        || (runtime
            .runtime_id
            .starts_with("callback-continuation-recovery-runtime-")
            && runtime.fallback_from_id.as_deref().is_some_and(|fallback| {
                fallback.starts_with("callback-continuation-recovery-runtime-")
            }));
    if !original_or_bound_fallback
        || runtime.session_id != record.commander_session_id
        || runtime.state != RuntimeState::Finished
    {
        return Err(anyhow!(
            "COMMANDER_CONVERGENCE_RUNTIME_IDENTITY_MISMATCH:{}",
            record.runtime_id
        ));
    }
    let output = runtime.output.as_ref().ok_or_else(|| {
        anyhow!(
            "COMMANDER_CONVERGENCE_RUNTIME_OUTPUT_MISSING:{}",
            record.runtime_id
        )
    })?;
    let proof: CommanderConvergenceProof = serde_json::from_value(
        output
            .get("commander_convergence_proof")
            .cloned()
            .ok_or_else(|| anyhow!("COMMANDER_CONVERGENCE_PROOF_MISSING:{}", record.runtime_id))?,
    )
    .map_err(|error| {
        anyhow!(
            "COMMANDER_CONVERGENCE_PROOF_MALFORMED:{}:{error}",
            record.runtime_id
        )
    })?;
    let final_content = output.get("content").ok_or_else(|| {
        anyhow!(
            "COMMANDER_CONVERGENCE_FINAL_ASSISTANT_MISSING:{}",
            record.runtime_id
        )
    })?;
    if canonical_value_sha256(final_content) != proof.final_assistant_sha256 {
        return Err(anyhow!(
            "COMMANDER_CONVERGENCE_FINAL_ASSISTANT_HASH_MISMATCH:{}",
            record.runtime_id
        ));
    }
    proof.validate_shape().map_err(anyhow::Error::msg)?;
    Ok(proof)
}

fn complete_and_ack_callback_continuation(
    store: &SessionLifecycleStore,
    record: &ContinuationDispatchRecord,
) -> Result<ContinuationWriteOutcome> {
    store.mark_callback_continuation_completed(record)?;
    store.acknowledge_callback(
        &record.child_transaction_id,
        &record.child_event_id,
        &record.callback_payload_sha256,
        &record.effect_identity,
    )?;
    store.acknowledge(
        &record.child_transaction_id,
        &record.child_event_id,
        &record.request_id,
    )?;
    Ok(store.mark_callback_continuation_acknowledged(record)?)
}

fn validate_direct_writer_continuation_chain(
    store: &SessionLifecycleStore,
    continuation: &ContinuationDispatchRecord,
    pending_callback: Option<&DurableCallbackRecord>,
) -> Result<()> {
    let admission = store
        .child_admission(&continuation.child_session_id)?
        .ok_or_else(|| {
            anyhow!(
                "CONTINUATION_CHILD_ADMISSION_NOT_FOUND:{}",
                continuation.child_session_id
            )
        })?;
    let callback = match pending_callback {
        Some(record) => record.clone(),
        None => store
            .intaken_callback(
                &continuation.child_transaction_id,
                &continuation.child_event_id,
            )?
            .ok_or_else(|| {
                anyhow!(
                    "CONTINUATION_INTAKEN_CALLBACK_NOT_FOUND:{}:{}",
                    continuation.child_transaction_id,
                    continuation.child_event_id
                )
            })?,
    };
    let route = Some(LifecycleCallbackDeliveryRoute::TrustedTuraDirectThreadWriter);
    if admission.commander_thread_id.is_none()
        || admission.callback_delivery_route != route
        || callback.commander_thread_id != admission.commander_thread_id
        || callback.callback_delivery_route != route
        || continuation.commander_thread_id != admission.commander_thread_id
        || continuation.callback_delivery_route != route
    {
        return Err(anyhow!(
            "CONTINUATION_DIRECT_WRITER_BINDING_MISMATCH:{}:{}",
            continuation.child_transaction_id,
            continuation.child_event_id
        ));
    }
    Ok(())
}

fn replay_terminal_callbacks_from_store(
    store: &SessionLifecycleStore,
    commander_session_id: &str,
    child_session_id: &str,
    transaction_id: &str,
) -> Result<Vec<(Value, TerminalDeliveryIdentity)>> {
    let mut matching = Vec::new();
    for record in store.callbacks_for_replay()? {
        if record.transaction_id != transaction_id {
            continue;
        }
        if record.commander_session_id != commander_session_id
            || record.child_session_id != child_session_id
        {
            return Err(anyhow!(
                "CALLBACK_REPLAY_IDENTITY_MISMATCH:commander={commander_session_id},session={child_session_id},transaction={transaction_id}"
            ));
        }
        let admission = store
            .child_admission(&record.child_session_id)?
            .ok_or_else(|| {
                anyhow!(
                    "CALLBACK_REPLAY_CHILD_ADMISSION_NOT_FOUND:{}",
                    record.child_session_id
                )
            })?;
        if admission.commander_thread_id.is_none()
            || admission.callback_delivery_route
                != Some(LifecycleCallbackDeliveryRoute::TrustedTuraDirectThreadWriter)
            || record.commander_thread_id != admission.commander_thread_id
            || record.callback_delivery_route != admission.callback_delivery_route
        {
            return Err(anyhow!(
                "CALLBACK_REPLAY_DIRECT_WRITER_BINDING_MISMATCH:{}:{}",
                record.transaction_id,
                record.event_id
            ));
        }
        store.mark_callback_intaken(
            &record.transaction_id,
            &record.event_id,
            &record.callback_payload_sha256,
        )?;
        matching.push(record);
    }
    matching
        .into_iter()
        .map(|record| {
            let delivery = TerminalDeliveryIdentity {
                commander_session_id: record.commander_session_id.clone(),
                transaction_id: record.transaction_id.clone(),
                event_id: record.event_id.clone(),
                runtime_id: record.runtime_id.clone(),
                callback_payload_sha256: Some(record.callback_payload_sha256.clone()),
                callback_effect_identity: Some(record.effect_identity.clone()),
            };
            Ok((record.transport_payload, delivery))
        })
        .collect()
}

fn publish_terminal_failure_callback_from_store(
    store: &SessionLifecycleStore,
    mut delivery: TerminalDeliveryIdentity,
) -> Result<Option<(Value, TerminalDeliveryIdentity)>> {
    let receipt = store.terminal_receipt(&delivery.transaction_id, &delivery.event_id)?;
    if receipt.child_session_id == receipt.commander_session_id
        || receipt.terminal_state == TerminalState::Completed
    {
        return Ok(None);
    }
    let parent_mission_revision_sha256 = receipt
        .audit_metadata
        .get("parent_mission_revision_sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            anyhow!("DELEGATED_LIFECYCLE_IDENTITY_MISSING:parent_mission_revision_sha256")
        })?;
    let delegated_input_sha256 = receipt
        .audit_metadata
        .get("delegated_input_sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("DELEGATED_LIFECYCLE_IDENTITY_MISSING:delegated_input_sha256"))?;
    let admission = require_terminal_callback_admission(
        store,
        &receipt,
        parent_mission_revision_sha256,
        delegated_input_sha256,
        None,
    )?;
    let receipt_sha256 = session_lifecycle::terminal_receipt_sha256(&receipt)?;
    let pre_execution_zero_effect = receipt.terminal_state == TerminalState::Interrupted
        && receipt
            .audit_metadata
            .get("runtime_state")
            .is_some_and(Value::is_null);
    let (classification, effect_identity) = if pre_execution_zero_effect {
        (
            "PROVEN_ZERO_EFFECT",
            CallbackEffectIdentity::ProvenZeroEffect {
                classification: "ADMITTED_PRE_EXECUTION_FAILURE".to_string(),
                evidence_sha256: receipt_sha256.clone(),
            },
        )
    } else {
        (
            "UNSETTLED_EFFECT",
            CallbackEffectIdentity::UnsettledEffect {
                classification: "terminal_receipt_without_settled_effect_evidence".to_string(),
                evidence_sha256: receipt_sha256.clone(),
            },
        )
    };
    let callback_payload = json!({
        "type": "terminal_failure",
        "classification": classification,
        "terminal_receipt_sha256": receipt_sha256,
        "runtime_id": receipt.runtime_id,
        "lease_id": receipt.lease_id,
        "terminal_state": receipt.terminal_state,
        "parent_mission_revision_sha256": parent_mission_revision_sha256,
    });
    let transport_payload = json!({
        "request_id": delivery.transaction_id,
        "kind": "gateway.callback",
        "method": "session.terminal_failure",
        "payload": {
            "session_id": receipt.child_session_id,
            "runtime_id": receipt.runtime_id,
            "body": { "item": callback_payload }
        }
    });
    let mut record = DurableCallbackRecord::new(
        &receipt,
        callback_payload,
        transport_payload,
        parent_mission_revision_sha256,
        delegated_input_sha256,
        effect_identity,
    )?;
    record.commander_thread_id = admission.commander_thread_id;
    record.callback_delivery_route = admission.callback_delivery_route;
    store.publish_callback(&record)?;
    store.mark_callback_intaken(
        &record.transaction_id,
        &record.event_id,
        &record.callback_payload_sha256,
    )?;
    delivery.callback_payload_sha256 = Some(record.callback_payload_sha256.clone());
    delivery.callback_effect_identity = Some(record.effect_identity.clone());
    Ok(Some((record.transport_payload, delivery)))
}

fn require_terminal_callback_admission(
    store: &SessionLifecycleStore,
    receipt: &TerminalReceipt,
    parent_mission_revision_sha256: &str,
    delegated_input_sha256: &str,
    exact_effect_id: Option<&str>,
) -> Result<ChildAdmissionRecord> {
    let admission = store
        .child_admission(&receipt.child_session_id)?
        .ok_or_else(|| {
            anyhow!(
                "TERMINAL_CALLBACK_CHILD_ADMISSION_NOT_DURABLE:{}",
                receipt.child_session_id
            )
        })?;
    if admission.parent_session_id != receipt.commander_session_id
        || admission.parent_mission_revision_sha256 != parent_mission_revision_sha256
        || admission.child_runtime_id != receipt.runtime_id
        || admission.child_transaction_id != receipt.transaction_id
        || admission.child_lease_id != receipt.lease_id
        || admission.callback_request_id != receipt.transaction_id
        || admission.delegated_input_sha256 != delegated_input_sha256
        || admission
            .commander_thread_id
            .as_deref()
            .is_none_or(|value| value.trim().is_empty())
        || admission.callback_delivery_route
            != Some(LifecycleCallbackDeliveryRoute::TrustedTuraDirectThreadWriter)
    {
        return Err(anyhow!(
            "TERMINAL_CALLBACK_ADMISSION_IDENTITY_MISMATCH:{}",
            receipt.child_session_id
        ));
    }
    if let Some(effect_id) = exact_effect_id
        && admission.effect_id != effect_id
    {
        return Err(anyhow!(
            "TERMINAL_CALLBACK_EFFECT_IDENTITY_CONFLICT:expected={},actual={effect_id}",
            admission.effect_id
        ));
    }
    Ok(admission)
}

fn intake_terminal_receipt(
    store: &SessionLifecycleStore,
    entry: &SessionFeedEntry,
    runtime_id: &str,
    transaction_id: &str,
    lease: &RuntimeLease,
    expected_terminal_state: TerminalState,
) -> Result<Option<TerminalDeliveryIdentity>> {
    let receipt = store.terminal_receipt(transaction_id, &entry.event_id)?;
    if receipt.commander_session_id != lease.commander_session_id
        || receipt.child_session_id != entry.session_id
        || receipt.runtime_id != runtime_id
        || receipt.terminal_state != expected_terminal_state
        || receipt.task_id != lease.task_id
        || receipt.goal_id != lease.goal_id
        || receipt.operator_override != lease.operator_override
        || receipt
            .audit_metadata
            .get("dispatch_runtime_id")
            .and_then(Value::as_str)
            != Some(lease.runtime_id.as_str())
        || receipt
            .audit_metadata
            .get("dispatch_lease_id")
            .and_then(Value::as_str)
            != Some(lease.lease_id.as_str())
        || (runtime_id == lease.runtime_id && receipt.lease_id != lease.lease_id)
    {
        return Err(anyhow!(
            "TERMINAL_RECEIPT_DISPATCH_IDENTITY_MISMATCH:session={},runtime={},transaction={},event={}",
            entry.session_id,
            runtime_id,
            transaction_id,
            entry.event_id
        ));
    }
    match store.intake(transaction_id, &entry.event_id)? {
        IntakeOutcome::Applied { .. } | IntakeOutcome::Duplicate { .. } => {
            Ok(Some(TerminalDeliveryIdentity {
                commander_session_id: lease.commander_session_id.clone(),
                transaction_id: transaction_id.to_string(),
                event_id: entry.event_id.clone(),
                runtime_id: runtime_id.to_string(),
                callback_payload_sha256: None,
                callback_effect_identity: None,
            }))
        }
        IntakeOutcome::Pending { blocker } => Err(anyhow!(blocker)),
    }
}

fn read_runtime_lease_snapshot(runtime_id: &str) -> Result<RuntimeLeaseSnapshot> {
    match session_log_contract::client::call_service(&SessionLogCommand::GetRuntimeLease(
        GetRuntimeLeaseRequest {
            runtime_id: runtime_id.to_string(),
            database_path: None,
        },
    ))? {
        SessionLogResponse::RuntimeLeaseRead {
            runtime: Some(snapshot),
        } => Ok(snapshot),
        SessionLogResponse::RuntimeLeaseRead { runtime: None } => Err(anyhow!(
            "RUNTIME_CALLBACK_DURABLE_LEASE_NOT_FOUND:{runtime_id}"
        )),
        SessionLogResponse::Error { error } => Err(anyhow!(error)),
        other => Err(anyhow!("RUNTIME_CALLBACK_LEASE_READ_UNEXPECTED:{other:?}")),
    }
}

fn runtime_lease_from_snapshot(snapshot: &RuntimeLeaseSnapshot) -> Result<RuntimeLease> {
    let lifecycle = snapshot.lifecycle.as_ref().ok_or_else(|| {
        anyhow!(
            "CRASH_RECOVERY_LACKS_DURABLE_LIFECYCLE_IDENTITY:{}",
            snapshot.runtime_id
        )
    })?;
    lifecycle
        .validate()
        .map_err(anyhow::Error::msg)
        .map_err(|error| {
            anyhow!(
                "RUNTIME_CALLBACK_DURABLE_LIFECYCLE_IDENTITY_INVALID:{}:{error}",
                snapshot.runtime_id
            )
        })?;
    let runtime_lease_id = snapshot.lease_id.as_deref().ok_or_else(|| {
        anyhow!(
            "RUNTIME_CALLBACK_DURABLE_LEASE_ID_MISSING:{}",
            snapshot.runtime_id
        )
    })?;
    if lifecycle.dispatch_runtime_id == snapshot.runtime_id
        && lifecycle.dispatch_lease_id != runtime_lease_id
    {
        return Err(anyhow!(
            "RUNTIME_CALLBACK_DISPATCH_LEASE_IDENTITY_MISMATCH:runtime={},durable_lease={},dispatch_lease={}",
            snapshot.runtime_id,
            runtime_lease_id,
            lifecycle.dispatch_lease_id
        ));
    }
    Ok(RuntimeLease {
        runtime_id: lifecycle.dispatch_runtime_id.clone(),
        lease_id: lifecycle.dispatch_lease_id.clone(),
        commander_session_id: lifecycle.commander_session_id.clone(),
        transaction_id: lifecycle.transaction_id.clone(),
        parent_mission_revision_sha256: lifecycle.parent_mission_revision_sha256.clone(),
        delegated_input_sha256: lifecycle.delegated_input_sha256.clone(),
        task_id: lifecycle.task_id.clone(),
        goal_id: lifecycle.goal_id.clone(),
        operator_override: lifecycle.operator_override,
        receipt_event_seq: lifecycle.receipt_event_seq,
        slot_acquired: false,
        terminalizing: true,
    })
}

fn runtime_terminal_state_from_snapshot(
    snapshot: &RuntimeLeaseSnapshot,
    projection: &lifecycle::SessionProjection,
) -> Result<TerminalState> {
    match snapshot.runtime_state {
        Some(state) => runtime_terminal_state(state),
        None => {
            if snapshot.session_state != projection.state {
                return Err(anyhow!(
                    "RUNTIME_CALLBACK_SESSION_STATE_MISMATCH:runtime={},snapshot={:?},projection={:?}",
                    snapshot.runtime_id,
                    snapshot.session_state,
                    projection.state
                ));
            }
            terminal_state(projection.state)
        }
    }
}

fn terminal_feed_entry_for_runtime(session_id: &str, runtime_id: &str) -> Result<SessionFeedEntry> {
    let mut after_cursor = 0;
    let mut latest = None;
    loop {
        let response = session_log_contract::client::call_service(
            &SessionLogCommand::ReadSessionFeed(session_log_contract::ReadSessionFeedRequest {
                session_id: session_id.to_string(),
                after_cursor,
                limit: 1_000,
            }),
        )?;
        let SessionLogResponse::SessionFeed {
            entries,
            next_cursor,
        } = response
        else {
            return Err(anyhow!("TERMINAL_FEED_READ_FAILED:{response:?}"));
        };
        for entry in entries {
            let is_terminal = entry.runtime_id.as_deref() == Some(runtime_id)
                && matches!(
                    &entry.event,
                    SessionFeedEvent::SessionProjectionUpdated { projection, .. }
                        if projection.state.is_terminal()
                );
            if is_terminal {
                latest = Some(entry);
            }
        }
        if next_cursor <= after_cursor {
            break;
        }
        after_cursor = next_cursor;
    }
    latest.ok_or_else(|| {
        anyhow!("TERMINAL_FEED_EVENT_NOT_FOUND:session={session_id},runtime={runtime_id}")
    })
}

fn terminal_runtime_is_current(
    projection: &lifecycle::SessionProjection,
    session_id: &str,
    dispatch_runtime_id: &str,
    runtime_id: &str,
) -> Result<bool> {
    let Some(dispatch_index) = projection
        .runtime_ids
        .iter()
        .position(|candidate| candidate == dispatch_runtime_id)
    else {
        return Ok(false);
    };
    let Some(runtime_index) = projection
        .runtime_ids
        .iter()
        .position(|candidate| candidate == runtime_id)
    else {
        return Err(anyhow!(
            "TERMINAL_FEED_RUNTIME_NOT_IN_SESSION:session={session_id},runtime={runtime_id}"
        ));
    };
    if runtime_index < dispatch_index {
        return Ok(false);
    }
    if projection.runtime_ids.last().map(String::as_str) != Some(runtime_id)
        || projection.session_id != session_id
    {
        return Err(anyhow!(
            "TERMINAL_FEED_RUNTIME_CHAIN_MISMATCH:session={session_id},dispatch_runtime={dispatch_runtime_id},runtime={runtime_id}"
        ));
    }
    Ok(true)
}

fn evaluate_task_ready_set_request(
    service: &ExecutionService,
    request: &ReadTaskReadySetRequest,
) -> Result<ReadTaskReadySetResponse> {
    let parent = read_session_snapshot(&request.parent_session_id)?.ok_or_else(|| {
        anyhow!(
            "TASK_READY_SET_PARENT_SESSION_NOT_FOUND:{}",
            request.parent_session_id
        )
    })?;
    let task_plan = &parent.lifecycle_projection.task_plan;
    let parent_task_plan_sha256 = task_plan_sha256(task_plan)?;
    let plan_identity_matches = request
        .expected_parent_task_plan_sha256
        .as_deref()
        .is_none_or(|expected| expected == parent_task_plan_sha256);
    let active_leases = service.sessions.lock().clone();
    let (base_evidence, active_identities) = collect_task_ready_set_evidence(
        &request.parent_session_id,
        task_plan,
        &request.authority_mission_revision_sha256,
        None,
        None,
        None,
        parent
            .lifecycle_projection
            .state
            .can_transition_to(lifecycle::SessionState::Running),
        service.runtime_slots.active_count(),
        &active_leases,
    );
    let mut entries = request
        .task_ids
        .iter()
        .map(|task_id| {
            task_ready_set_entry(
                task_plan,
                task_id,
                &parent_task_plan_sha256,
                plan_identity_matches,
                &base_evidence,
                &active_identities,
            )
        })
        .collect::<Vec<_>>();
    entries.sort_by(|left, right| left.task_id.cmp(&right.task_id));
    for entry in &mut entries {
        entry.reason_codes.sort();
        entry.reason_codes.dedup();
        entry.blocking_task_ids.sort();
        entry.blocking_task_ids.dedup();
        entry.dependency_task_ids.sort();
        entry.dependency_task_ids.dedup();
        entry.active_lease_ids.sort();
        entry.active_lease_ids.dedup();
    }
    let identity = json!({
        "schema_version": "tura_task_ready_set_response_v1",
        "parent_session_id": request.parent_session_id,
        "parent_task_plan_sha256": parent_task_plan_sha256,
        "authority_mission_revision_sha256": request.authority_mission_revision_sha256,
        "entries": entries,
        "authority_effect": "none",
    });
    let state_head = canonical_value_sha256(&identity);
    Ok(ReadTaskReadySetResponse {
        schema_version: "tura_task_ready_set_response_v1".to_string(),
        parent_session_id: request.parent_session_id.clone(),
        parent_task_plan_sha256,
        state_head,
        authority_effect: "none".to_string(),
        entries,
    })
}

fn collect_task_ready_set_evidence(
    commander_session_id: &str,
    task_plan: &lifecycle::TaskPlan,
    authority_mission_revision_sha256: &str,
    requested_lease_id: Option<String>,
    available_exact_input_sha256s: Option<BTreeSet<String>>,
    claimed_task_id: Option<String>,
    parent_session_can_transition_to_running: bool,
    active_runtime_workers: usize,
    active_leases: &HashMap<String, RuntimeLease>,
) -> (
    TaskReadySetEvidence,
    BTreeMap<String, ChildReadySetIdentity>,
) {
    let mut execution_terminal_task_ids = BTreeSet::new();
    let lifecycle_projection = lifecycle_store_if_exists(commander_session_id)
        .ok()
        .flatten()
        .and_then(|store| store.control_deck_projection().ok());
    if let Some(projection) = lifecycle_projection.as_ref() {
        for task in &task_plan.detailed_tasks {
            let Some(contract) = task.scheduling_contract.as_ref() else {
                continue;
            };
            let contract_sha256 = canonical_value_sha256(
                &serde_json::to_value(contract).expect("scheduling contract is serializable"),
            );
            let Some(admission) = projection.admissions.iter().find(|admission| {
                admission.parent_mission_revision_sha256
                    == contract.authority_mission_revision_sha256
                    && admission
                        .ready_set_identity
                        .as_ref()
                        .is_some_and(|identity| {
                            identity.task_id == task.task_id
                                && identity.mission_id == contract.mission_id
                                && identity.task_scheduling_contract_sha256 == contract_sha256
                        })
                    && (task.sub_session_id.is_empty()
                        || task.sub_session_id == admission.child_session_id)
            }) else {
                continue;
            };
            let exact_callbacks = projection
                .callbacks
                .iter()
                .filter(|callback| callback.child_session_id == admission.child_session_id)
                .filter(|callback| {
                    callback_matches_trusted_direct_writer_admission(callback, admission)
                })
                .collect::<Vec<_>>();
            if exact_callbacks
                .iter()
                .any(|callback| callback_proves_terminal_execution(callback))
            {
                execution_terminal_task_ids.insert(task.task_id.clone());
            }
        }
    }

    let mut task_leases = Vec::new();
    let mut lease_unknown_task_ids = BTreeSet::new();
    let mut active_identities = BTreeMap::new();
    for (child_session_id, lease) in active_leases {
        if !ready_set_tracks_runtime_lease(commander_session_id, child_session_id, lease) {
            continue;
        }
        let local_task_id = lease
            .task_id
            .clone()
            .unwrap_or_else(|| format!("unbound:{child_session_id}"));
        let evidence_task_id = if lease.commander_session_id == commander_session_id {
            local_task_id.clone()
        } else {
            format!("{}:{local_task_id}", lease.commander_session_id)
        };
        match read_runtime_lease_snapshot(&lease.runtime_id) {
            Ok(runtime)
                if runtime.session_id == *child_session_id
                    && runtime.runtime_id == lease.runtime_id
                    && runtime.lease_id.as_deref() == Some(lease.lease_id.as_str())
                    && runtime.lease_active
                    && !runtime.terminal
                    && runtime.lifecycle.as_ref().is_some_and(|identity| {
                        identity.commander_session_id == lease.commander_session_id
                            && identity.transaction_id == lease.transaction_id
                            && identity.parent_mission_revision_sha256
                                == lease.parent_mission_revision_sha256
                            && identity.delegated_input_sha256 == lease.delegated_input_sha256
                            && identity.task_id == lease.task_id
                            && identity.dispatch_runtime_id == lease.runtime_id
                            && identity.dispatch_lease_id == lease.lease_id
                    }) =>
            {
                task_leases.push(TaskLeaseReadinessEvidence {
                    task_id: evidence_task_id.clone(),
                    lease_id: lease.lease_id.clone(),
                    lease_active: true,
                    terminal: false,
                });
                let admission = lifecycle_store_if_exists(&lease.commander_session_id)
                    .ok()
                    .flatten()
                    .and_then(|store| store.child_admission(child_session_id).ok().flatten());
                match admission {
                    Some(record)
                        if record.ready_set_identity.as_ref().is_some_and(|identity| {
                            lease.task_id.as_deref() == Some(identity.task_id.as_str())
                        }) && record.parent_session_id == lease.commander_session_id
                            && record.child_session_id == *child_session_id
                            && record.child_runtime_id == lease.runtime_id
                            && record.child_transaction_id == lease.transaction_id
                            && record.child_lease_id == lease.lease_id
                            && record.parent_mission_revision_sha256.as_str()
                                == lease
                                    .parent_mission_revision_sha256
                                    .as_deref()
                                    .unwrap_or("")
                            && record.delegated_input_sha256.as_str()
                                == lease.delegated_input_sha256.as_deref().unwrap_or("") =>
                    {
                        active_identities.insert(
                            evidence_task_id,
                            record
                                .ready_set_identity
                                .expect("guard requires ready-set identity"),
                        );
                    }
                    _ => {
                        lease_unknown_task_ids.insert(evidence_task_id);
                    }
                }
            }
            Ok(runtime) if !runtime.lease_active && runtime.terminal => {}
            Ok(_) | Err(_) => {
                lease_unknown_task_ids.insert(evidence_task_id);
            }
        }
    }
    task_leases.sort_by(|left, right| {
        (&left.task_id, &left.lease_id).cmp(&(&right.task_id, &right.lease_id))
    });
    let evidence = TaskReadySetEvidence {
        evaluated_at_ms: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_millis().min(i64::MAX as u128) as i64)
            .unwrap_or_default(),
        parent_session_can_transition_to_running,
        authority_mission_revision_sha256: authority_mission_revision_sha256.to_string(),
        execution_terminal_task_ids,
        available_exact_input_sha256s,
        semantic_dispatch_identity_valid: true,
        candidate_scope_identity_valid: true,
        parallel_runtime_limit_supported: true,
        active_runtime_workers,
        task_leases,
        lease_unknown_task_ids,
        scope_evaluated_task_ids: BTreeSet::new(),
        scope_conflicting_task_ids: BTreeSet::new(),
        scope_unknown_task_ids: BTreeSet::new(),
        requested_lease_id,
        claimed_task_id,
    };
    (evidence, active_identities)
}

fn callback_proves_terminal_execution(callback: &LifecycleCallbackProjection) -> bool {
    matches!(
        callback.effect_kind.as_str(),
        "exact" | "proven_zero_effect"
    )
}

fn callback_matches_trusted_direct_writer_admission(
    callback: &LifecycleCallbackProjection,
    admission: &LifecycleAdmissionProjection,
) -> bool {
    admission.callback_delivery_route
        == Some(LifecycleCallbackDeliveryRoute::TrustedTuraDirectThreadWriter)
        && admission
            .commander_thread_id
            .as_deref()
            .is_some_and(|thread_id| !thread_id.trim().is_empty())
        && callback.transaction_id == admission.child_transaction_id
        && callback.runtime_id == admission.child_runtime_id
        && callback.lease_id == admission.child_lease_id
        && callback.parent_mission_revision_sha256 == admission.parent_mission_revision_sha256
        && callback.commander_thread_id == admission.commander_thread_id
        && callback.callback_delivery_route
            == Some(LifecycleCallbackDeliveryRoute::TrustedTuraDirectThreadWriter)
        && callback.delegated_input_sha256 == admission.delegated_input_sha256
}

fn ready_set_tracks_runtime_lease(
    commander_session_id: &str,
    child_session_id: &str,
    _lease: &RuntimeLease,
) -> bool {
    // Terminalizing remains an owner until the durable lease is inactive and terminal.
    child_session_id != commander_session_id
}

fn task_ready_set_entry(
    task_plan: &lifecycle::TaskPlan,
    task_id: &str,
    parent_task_plan_sha256: &str,
    plan_identity_matches: bool,
    base_evidence: &TaskReadySetEvidence,
    active_identities: &BTreeMap<String, ChildReadySetIdentity>,
) -> TaskReadySetEntry {
    let task = task_plan
        .detailed_tasks
        .iter()
        .find(|candidate| candidate.task_id == task_id);
    let contract = task.and_then(|task| task.scheduling_contract.as_ref());
    let mut evidence = base_evidence.clone();
    if let Some(candidate) = contract {
        evidence.semantic_dispatch_identity_valid =
            canonical_value_sha256(&candidate.semantic_dispatch_identity(task_id))
                == candidate.semantic_dispatch_key;
        evidence.parallel_runtime_limit_supported =
            maximum_parallel_runtime_workers(Some(candidate.maximum_parallel_runtime_workers))
                == candidate.maximum_parallel_runtime_workers;
        evidence.candidate_scope_identity_valid = tura_path::jspace::canonical_scope_projection(
            &candidate.read_scopes,
            &candidate.write_scopes,
        )
        .is_ok_and(|projection| {
            projection.read_scopes == candidate.read_scopes
                && projection.write_scopes == candidate.write_scopes
        })
            && tura_path::jspace::canonical_declared_targets(&candidate.declared_targets)
                .is_ok_and(|targets| targets == candidate.declared_targets);
        populate_scope_evidence(candidate, &mut evidence, active_identities);
    }
    let decision = if plan_identity_matches {
        classify_task_ready_set(task_plan, task_id, &evidence)
    } else {
        TaskReadySetDecision::typed_unknown("TASK_READY_SET_PARENT_PLAN_IDENTITY_MISMATCH")
    };
    let contract_sha256 = contract
        .and_then(|contract| serde_json::to_value(contract).ok())
        .map(|value| canonical_value_sha256(&value));
    let scope_claim_sha256 = contract.map(scope_claim_sha256);
    let mut active_lease_ids = evidence
        .task_leases
        .iter()
        .filter(|lease| lease.lease_active && !lease.terminal)
        .map(|lease| lease.lease_id.clone())
        .collect::<Vec<_>>();
    active_lease_ids.sort();
    active_lease_ids.dedup();
    let _ = parent_task_plan_sha256;
    TaskReadySetEntry {
        task_id: task_id.to_string(),
        state: ready_set_wire_state(decision.state),
        reason_codes: decision.reason_codes,
        blocking_task_ids: decision.blocking_task_ids,
        dependency_task_ids: contract
            .map(|contract| contract.dependency_task_ids.clone())
            .unwrap_or_default(),
        active_runtime_count: evidence.active_runtime_workers,
        active_lease_ids,
        semantic_dispatch_key: contract.map(|contract| contract.semantic_dispatch_key.clone()),
        task_scheduling_contract_sha256: contract_sha256,
        scope_claim_sha256,
        maximum_parallel_runtime_workers: contract
            .map(|contract| contract.maximum_parallel_runtime_workers),
    }
}

fn populate_scope_evidence(
    candidate: &TaskSchedulingContractV1,
    evidence: &mut TaskReadySetEvidence,
    active_identities: &BTreeMap<String, ChildReadySetIdentity>,
) {
    for lease in evidence
        .task_leases
        .iter()
        .filter(|lease| lease.lease_active && !lease.terminal)
    {
        let Some(active_identity) = active_identities.get(&lease.task_id) else {
            evidence
                .scope_unknown_task_ids
                .insert(lease.task_id.clone());
            continue;
        };
        let explicit_conflict = candidate.conflict_identities.iter().any(|identity| {
            active_identity
                .conflict_identities
                .binary_search(identity)
                .is_ok()
        });
        let left = JSpaceScopeProjection {
            read_scopes: candidate.read_scopes.clone(),
            write_scopes: candidate.write_scopes.clone(),
        };
        let right = JSpaceScopeProjection {
            read_scopes: active_identity.read_scopes.clone(),
            write_scopes: active_identity.write_scopes.clone(),
        };
        match scope_projections_conflict(&left, &right) {
            Ok(conflict) => {
                evidence
                    .scope_evaluated_task_ids
                    .insert(lease.task_id.clone());
                if explicit_conflict || conflict {
                    evidence
                        .scope_conflicting_task_ids
                        .insert(lease.task_id.clone());
                }
            }
            Err(_) => {
                evidence
                    .scope_unknown_task_ids
                    .insert(lease.task_id.clone());
            }
        }
    }
}

fn task_plan_sha256(task_plan: &lifecycle::TaskPlan) -> Result<String> {
    Ok(lifecycle::task_plan_ready_set_sha256(task_plan))
}

fn scope_claim_sha256(contract: &TaskSchedulingContractV1) -> String {
    contract.scope_claim_sha256()
}

fn ready_set_wire_state(state: TaskReadySetState) -> TaskReadySetWireState {
    match state {
        TaskReadySetState::Ready => TaskReadySetWireState::Ready,
        TaskReadySetState::BlockedState => TaskReadySetWireState::BlockedState,
        TaskReadySetState::BlockedDependency => TaskReadySetWireState::BlockedDependency,
        TaskReadySetState::BlockedScope => TaskReadySetWireState::BlockedScope,
        TaskReadySetState::BlockedLease => TaskReadySetWireState::BlockedLease,
        TaskReadySetState::BlockedCapacity => TaskReadySetWireState::BlockedCapacity,
        TaskReadySetState::TypedUnknown => TaskReadySetWireState::TypedUnknown,
    }
}

fn is_historical_terminal_runtime(active_runtime_id: Option<&str>, runtime_id: &str) -> bool {
    active_runtime_id != Some(runtime_id)
}

#[derive(Clone, Default)]
struct RuntimeSlotGate {
    active: Arc<Mutex<usize>>,
    notify: Arc<Notify>,
}

impl RuntimeSlotGate {
    fn active_count(&self) -> usize {
        *self.active.lock()
    }

    async fn acquire(&self, limit: usize) -> RuntimeSlotPermit {
        loop {
            let notified = self.notify.notified();
            {
                let mut active = self.active.lock();
                if *active < limit {
                    *active += 1;
                    return RuntimeSlotPermit { gate: self.clone() };
                }
            }
            notified.await;
        }
    }
}

struct RuntimeSlotPermit {
    gate: RuntimeSlotGate,
}

impl Drop for RuntimeSlotPermit {
    fn drop(&mut self) {
        let mut active = self.gate.active.lock();
        *active = active.saturating_sub(1);
        drop(active);
        self.gate.notify.notify_waiters();
    }
}

struct ActiveSessionGuard {
    sessions: Arc<Mutex<HashMap<String, RuntimeLease>>>,
    session_id: String,
    runtime_id: RuntimeId,
    active: std::sync::atomic::AtomicBool,
}

impl ActiveSessionGuard {
    fn new(
        sessions: Arc<Mutex<HashMap<String, RuntimeLease>>>,
        session_id: &str,
        runtime_id: &str,
    ) -> Self {
        Self {
            sessions,
            session_id: session_id.to_string(),
            runtime_id: runtime_id.to_string(),
            active: std::sync::atomic::AtomicBool::new(true),
        }
    }

    fn finish(&self) {
        self.active
            .store(false, std::sync::atomic::Ordering::SeqCst);
        self.remove_matching_lease();
    }

    fn retain(&self) {
        self.active
            .store(false, std::sync::atomic::Ordering::SeqCst);
    }

    fn remove_matching_lease(&self) {
        let mut sessions = self.sessions.lock();
        if sessions
            .get(&self.session_id)
            .is_some_and(|lease| lease.runtime_id == self.runtime_id)
        {
            sessions.remove(&self.session_id);
        }
    }
}

impl Drop for ActiveSessionGuard {
    fn drop(&mut self) {
        if self.active.load(std::sync::atomic::Ordering::SeqCst) {
            self.remove_matching_lease();
        }
    }
}

fn payload_to_run_agent_request(
    request: &EnqueueTurnRequest,
    lease_id: &str,
    fallback_from_id: Option<String>,
) -> Result<RunAgentRequest> {
    let mut value = request.payload.clone();
    if let Some(object) = value.as_object_mut() {
        object.insert(
            "session_id".to_string(),
            Value::String(request.session_id.clone()),
        );
        object.insert(
            "runtime_id".to_string(),
            Value::String(request.runtime_id.clone()),
        );
        object.insert("lease_id".to_string(), Value::String(lease_id.to_string()));
        match fallback_from_id {
            Some(fallback_from_id) => {
                object.insert(
                    "fallback_from_id".to_string(),
                    Value::String(fallback_from_id),
                );
            }
            None => {
                object.remove("fallback_from_id");
            }
        }
    }
    serde_json::from_value(value).map_err(|error| {
        anyhow!(
            "invalid run-agent payload for runtime {} session {}: {error}",
            request.runtime_id,
            request.session_id
        )
    })
}

fn validate_delegated_input_digest(request: &mut RunAgentRequest, delegated: bool) -> Result<()> {
    if !delegated {
        return Ok(());
    }
    let recomputed_digest = request
        .effective_prompt_sha256()
        .ok_or_else(|| anyhow!("DELEGATED_LIFECYCLE_IDENTITY_MISSING:effective_prompt"))?;
    if request.delegated_input_sha256.as_deref() != Some(&recomputed_digest) {
        return Err(anyhow!(
            "DELEGATED_INPUT_SHA256_MISMATCH:expected={recomputed_digest},actual={}",
            request
                .delegated_input_sha256
                .as_deref()
                .unwrap_or("missing")
        ));
    }
    request.delegated_input_sha256 = Some(recomputed_digest);
    Ok(())
}

fn read_session_snapshot(session_id: &str) -> Result<Option<SessionSnapshot>> {
    match session_log_contract::client::call_service(&SessionLogCommand::GetSession(
        GetSessionRequest {
            session_id: session_id.to_string(),
        },
    ))? {
        SessionLogResponse::Session { session } => Ok(session.map(|session| *session)),
        SessionLogResponse::Error { error } => Err(anyhow!(error)),
        other => Err(anyhow!(
            "unexpected session_db response while reading {session_id}: {other:?}"
        )),
    }
}

pub(crate) struct CompiledCommanderTaskPacket {
    pub(crate) result: CommanderTaskPacketCompileResult,
    pub(crate) request: RegisterChildSessionRequest,
}

struct CommanderCompileParent<'a> {
    session_id: &'a str,
    workspace: &'a str,
    model: Option<&'a str>,
    agent: Option<&'a str>,
    session_type: &'a str,
    task_plan: &'a TaskPlan,
}

#[derive(Debug)]
struct PreClaimChildAdmissionError {
    message: String,
}

impl std::fmt::Display for PreClaimChildAdmissionError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for PreClaimChildAdmissionError {}

struct PreparedChildReadySet {
    task_id: String,
    contract: TaskSchedulingContractV1,
    existing_claim: bool,
    parent_task_plan_sha256: String,
    task_scheduling_contract_sha256: String,
    computed_scope_claim_sha256: String,
    capsule_semantic_sha256: String,
    claim: TaskDispatchClaimV1,
}

async fn validate_commander_execution_authority(
    state: &AppState,
    packet: &CommanderTaskPacketV1,
    parent: &SessionSnapshot,
) -> Result<()> {
    let model = packet
        .model
        .as_deref()
        .or(parent.metadata.model.as_deref())
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| anyhow!("TASK_PACKET_MODEL_IDENTITY_MISSING"))?;
    let settings = tura_llm_rust::Settings::default()
        .await
        .map_err(|error| anyhow!("TASK_PACKET_PROVIDER_SETTINGS_UNAVAILABLE:{error}"))?;
    settings
        .validate_session_model_override(model)
        .map_err(|error| anyhow!("TASK_PACKET_MODEL_AUTHORITY_REJECTED:{error}"))?;

    if let Some(agent_id) = packet
        .agent
        .as_deref()
        .or(parent.metadata.agent.as_deref())
        .filter(|value| !value.trim().is_empty())
    {
        let session_type = packet
            .session_type
            .as_deref()
            .unwrap_or(&parent.metadata.session_type);
        state
            .registry
            .agents
            .resolve_for_project(
                Some(agent_id),
                Some(session_type),
                Some(Path::new(&parent.workspace)),
            )
            .map_err(|_| anyhow!("TASK_PACKET_AGENT_NOT_FOUND:{agent_id}"))?;
    }
    Ok(())
}

fn compile_commander_task_packet(
    packet: &CommanderTaskPacketV1,
    parent: &SessionSnapshot,
) -> Result<CompiledCommanderTaskPacket> {
    packet.validate_shape().map_err(anyhow::Error::msg)?;
    parent.validate().map_err(anyhow::Error::msg)?;
    compile_commander_task_packet_components(
        packet,
        CommanderCompileParent {
            session_id: &parent.session_id,
            workspace: &parent.workspace,
            model: parent.metadata.model.as_deref(),
            agent: parent.metadata.agent.as_deref(),
            session_type: &parent.metadata.session_type,
            task_plan: &parent.lifecycle_projection.task_plan,
        },
    )
}

fn compile_commander_task_packet_components(
    packet: &CommanderTaskPacketV1,
    parent: CommanderCompileParent<'_>,
) -> Result<CompiledCommanderTaskPacket> {
    packet.validate_shape().map_err(anyhow::Error::msg)?;
    if parent.session_id != packet.parent_session_id {
        return Err(anyhow!("TASK_PACKET_PARENT_SESSION_ID_MISMATCH"));
    }
    let session_directory = Path::new(&packet.session_directory);
    if !session_directory.is_absolute()
        || session_directory
            .components()
            .any(|component| matches!(component, std::path::Component::ParentDir))
        || !session_directory.starts_with(Path::new(parent.workspace))
    {
        return Err(anyhow!(
            "TASK_PACKET_SESSION_DIRECTORY_OUTSIDE_PARENT_WORKSPACE"
        ));
    }

    let capsule = TaskContextCapsule::from_value(packet.task_context_capsule.clone())
        .map_err(anyhow::Error::msg)?;
    capsule
        .bind_jspace(Some(&packet.jspace_contract))
        .map_err(anyhow::Error::msg)?;
    if capsule.mission.task_id.as_deref() != Some(packet.task_id.as_str()) {
        return Err(anyhow!("TASK_PACKET_CAPSULE_TASK_ID_MISMATCH"));
    }

    let task_plan = parent.task_plan;
    let task = task_plan
        .detailed_tasks
        .iter()
        .find(|task| task.task_id == packet.task_id)
        .ok_or_else(|| anyhow!("TASK_PACKET_TASK_NOT_FOUND:{}", packet.task_id))?;
    let contract = task
        .scheduling_contract
        .as_ref()
        .ok_or_else(|| anyhow!("TASK_PACKET_SCHEDULING_CONTRACT_MISSING:{}", packet.task_id))?;
    contract
        .validate(&packet.task_id)
        .map_err(anyhow::Error::msg)?;
    if contract.semantic_dispatch_key != contract.semantic_dispatch_sha256(&packet.task_id) {
        return Err(anyhow!("TASK_PACKET_SEMANTIC_DISPATCH_KEY_MISMATCH"));
    }
    if contract.authority_mission_revision_sha256 != packet.parent_mission_revision_sha256 {
        return Err(anyhow!("TASK_PACKET_MISSION_REVISION_MISMATCH"));
    }
    if contract.mission_id != capsule.mission.mission_id {
        return Err(anyhow!("TASK_PACKET_MISSION_ID_MISMATCH"));
    }
    if contract.task_context_capsule_semantic_sha256 != capsule.semantic_sha256 {
        return Err(anyhow!("TASK_PACKET_CAPSULE_BINDING_MISMATCH"));
    }

    let delegated_input_sha256 = canonical_value_sha256(&Value::String(packet.prompt.clone()));
    if contract.delegated_input_sha256 != delegated_input_sha256 {
        return Err(anyhow!("TASK_PACKET_DELEGATED_INPUT_DIGEST_MISMATCH"));
    }
    let mut exact_input_sha256s = capsule
        .evidence_refs
        .iter()
        .map(|reference| {
            reference
                .sha256
                .clone()
                .ok_or_else(|| anyhow!("TASK_PACKET_EVIDENCE_DIGEST_MISSING:{}", reference.id))
        })
        .collect::<Result<Vec<_>>>()?;
    exact_input_sha256s.sort();
    exact_input_sha256s.dedup();
    if exact_input_sha256s != contract.exact_input_sha256s {
        return Err(anyhow!("TASK_PACKET_EVIDENCE_DIGEST_MISMATCH"));
    }
    if let Some(binding) = &packet.native_codex_execution {
        binding
            .bind_authoritative_task(
                &capsule,
                &packet.task_id,
                &packet.parent_mission_revision_sha256,
                &packet.prompt,
            )
            .map_err(anyhow::Error::msg)?;
    }

    let matcher = JSpaceMatcher::from_value(Path::new(parent.workspace), &packet.jspace_contract)
        .map_err(|error| anyhow!("{}:{}", error.code(), error))?;
    if matcher.authorization_semantic_sha256() != contract.jspace_authorization_semantic_sha256
        || matcher.scope_projection().read_scopes != contract.read_scopes
        || matcher.scope_projection().write_scopes != contract.write_scopes
    {
        return Err(anyhow!("TASK_PACKET_JSPACE_SCOPE_BINDING_MISMATCH"));
    }
    if matcher.declared_targets() != contract.declared_targets {
        return Err(anyhow!("TASK_PACKET_JSPACE_TARGET_BINDING_MISMATCH"));
    }
    if packet.maximum_parallel_runtime_workers != contract.maximum_parallel_runtime_workers {
        return Err(anyhow!("TASK_PACKET_PARALLEL_LIMIT_MISMATCH"));
    }

    if let (Some(packet_model), Some(parent_model)) = (packet.model.as_deref(), parent.model)
        && packet_model != parent_model
    {
        return Err(anyhow!("TASK_PACKET_MODEL_IDENTITY_MISMATCH"));
    }
    if let (Some(packet_agent), Some(parent_agent)) = (packet.agent.as_deref(), parent.agent)
        && packet_agent != parent_agent
    {
        return Err(anyhow!("TASK_PACKET_AGENT_IDENTITY_MISMATCH"));
    }
    let model = packet
        .model
        .clone()
        .or_else(|| parent.model.map(str::to_string));
    if model.as_deref().is_none_or(|value| value.trim().is_empty()) {
        return Err(anyhow!("TASK_PACKET_MODEL_IDENTITY_MISSING"));
    }
    let agent = packet
        .agent
        .clone()
        .or_else(|| parent.agent.map(str::to_string));
    let session_type = packet
        .session_type
        .clone()
        .unwrap_or_else(|| parent.session_type.to_string());

    let task_scheduling_contract_sha256 = contract.contract_sha256();
    let scope_claim_sha256 = contract.scope_claim_sha256();
    let parent_task_plan_sha256 = if let Some(claim) = task.dispatch_claim.as_ref() {
        claim.validate().map_err(anyhow::Error::msg)?;
        if claim.mission_id != contract.mission_id
            || claim.task_id != packet.task_id
            || claim.semantic_dispatch_key != contract.semantic_dispatch_key
            || claim.authority_mission_revision_sha256 != packet.parent_mission_revision_sha256
            || claim.delegated_input_sha256 != delegated_input_sha256
            || claim.task_context_capsule_semantic_sha256 != capsule.semantic_sha256
            || claim.task_scheduling_contract_sha256 != task_scheduling_contract_sha256
            || claim.scope_claim_sha256 != scope_claim_sha256
            || task.sub_session_id != claim.child_session_id
        {
            return Err(anyhow!("TASK_PACKET_DURABLE_CLAIM_MISMATCH"));
        }
        claim.parent_task_plan_sha256.clone()
    } else {
        task_plan_sha256(task_plan)?
    };
    let packet_sha256 = canonical_value_sha256(&serde_json::to_value(packet)?);
    let compile_identity_sha256 = canonical_value_sha256(&json!({
        "schema_version": "tura_commander_task_packet_compile_identity_v1",
        "protocol_version": COMMANDER_DISPATCH_PROTOCOL_VERSION,
        "packet_sha256": packet_sha256,
        "parent_task_plan_sha256": parent_task_plan_sha256,
        "semantic_dispatch_key": contract.semantic_dispatch_key,
        "task_scheduling_contract_sha256": task_scheduling_contract_sha256,
        "scope_claim_sha256": scope_claim_sha256,
    }));
    let child_session_id = format!("child-{compile_identity_sha256}");
    let child_runtime_id = format!("runtime-{compile_identity_sha256}");
    let child_transaction_id = format!("transaction-{compile_identity_sha256}");
    let child_lease_id = format!("lease-{compile_identity_sha256}");
    let callback_request_id = child_transaction_id.clone();
    let effect_id = format!("{child_runtime_id}.message");
    let callback_delivery_route = WireCallbackDeliveryRoute::TrustedTuraDirectThreadWriter;
    let mut worker_env = serde_json::Map::from_iter([(
        crate::runtime_dispatch::PROJECT_ROOT_ENV.to_string(),
        Value::String(parent.workspace.to_string()),
    )]);
    if let Some(service_tier) = packet.service_tier {
        worker_env.insert(
            runtime_contract::SESSION_SERVICE_TIER_ENV.to_string(),
            Value::String(service_tier.as_str().to_string()),
        );
    }

    let request = RegisterChildSessionRequest {
        parent_session_id: packet.parent_session_id.clone(),
        parent_mission_revision_sha256: packet.parent_mission_revision_sha256.clone(),
        commander_thread_id: Some(packet.commander_thread_id.clone()),
        child_session_id: child_session_id.clone(),
        child_runtime_id: child_runtime_id.clone(),
        child_transaction_id: child_transaction_id.clone(),
        child_lease_id: child_lease_id.clone(),
        callback_request_id: callback_request_id.clone(),
        effect_id: effect_id.clone(),
        callback_delivery_route,
        delegated_input_sha256: delegated_input_sha256.clone(),
        session_directory: packet.session_directory.clone(),
        session_name: packet.session_name.clone(),
        created_at_ms: packet.created_at_ms,
        execution_payload: json!({
            "task_id": packet.task_id,
            "prompt": packet.prompt,
            "directory": packet.session_directory,
            "model": model,
            "agent": agent,
            "session_type": session_type,
            "worker_env": worker_env,
            "maximum_parallel_runtime_workers": packet.maximum_parallel_runtime_workers,
            "task_context_capsule": packet.task_context_capsule,
            "jspace_contract": packet.jspace_contract,
            "native_codex_execution": packet.native_codex_execution,
        }),
    };
    request.validate().map_err(anyhow::Error::msg)?;
    let result = CommanderTaskPacketCompileResult {
        schema_version: "tura_commander_task_packet_compile_result_v1".to_string(),
        protocol_version: COMMANDER_DISPATCH_PROTOCOL_VERSION.to_string(),
        task_packet_schema_version: COMMANDER_TASK_PACKET_SCHEMA_VERSION.to_string(),
        compile_identity_sha256,
        semantic_dispatch_key: contract.semantic_dispatch_key.clone(),
        parent_task_plan_sha256,
        task_scheduling_contract_sha256,
        scope_claim_sha256,
        task_context_capsule_semantic_sha256: capsule.semantic_sha256,
        jspace_authorization_semantic_sha256: matcher.authorization_semantic_sha256().to_string(),
        delegated_input_sha256,
        parent_session_id: packet.parent_session_id.clone(),
        parent_mission_revision_sha256: packet.parent_mission_revision_sha256.clone(),
        commander_thread_id: packet.commander_thread_id.clone(),
        task_id: packet.task_id.clone(),
        child_session_id,
        child_runtime_id,
        child_transaction_id,
        child_lease_id,
        callback_request_id,
        effect_id,
        callback_delivery_route,
        authority_effect: "none".to_string(),
        mutation_counts: CommanderMutationCounts::zero(),
    };
    Ok(CompiledCommanderTaskPacket { result, request })
}

fn create_child_session(
    parent: &SessionSnapshot,
    request: &RegisterChildSessionRequest,
) -> Result<()> {
    let metadata = &parent.metadata;
    let command = SessionLogCommand::CreateSession(Box::new(CreateSessionRequest {
        command_id: format!("child-admission-create-{}", request.callback_request_id),
        session_id: request.child_session_id.clone(),
        creation_command: SessionCommand::RegisterChildSession {
            parent_id: request.parent_session_id.clone(),
        },
        copy_context: false,
        workspace: parent.workspace.clone(),
        session_directory: request.session_directory.clone(),
        name: request.session_name.clone(),
        created_at: request.created_at_ms,
        model: metadata.model.clone(),
        agent: metadata.agent.clone(),
        session_type: metadata.session_type.clone(),
        kill_processes_on_start: metadata.kill_processes_on_start,
        validator_enabled: metadata.validator_enabled,
        force_planning: metadata.force_planning,
        model_variant: metadata.model_variant.clone(),
        model_acceleration_enabled: metadata.model_acceleration_enabled,
        disable_permission_restrictions: metadata.disable_permission_restrictions,
        use_last_tool_call_response: metadata.use_last_tool_call_response,
        auto_session_name: false,
        initial_task_plan_patch: None,
    }));
    match session_log_contract::client::call_service(&command)? {
        SessionLogResponse::SessionCommandApplied { result }
            if result.projection.parent_id.as_deref()
                == Some(request.parent_session_id.as_str()) =>
        {
            Ok(())
        }
        SessionLogResponse::SessionCommandApplied { .. } => Err(anyhow!(
            "CHILD_ADMISSION_PARENT_IDENTITY_MISMATCH:{}",
            request.child_session_id
        )),
        SessionLogResponse::Error { error } => Err(anyhow!(error)),
        other => Err(anyhow!(
            "unexpected session_db child creation response: {other:?}"
        )),
    }
}

fn ensure_child_parent_identity(child: &SessionSnapshot, parent_session_id: &str) -> Result<()> {
    if child.lifecycle_projection.parent_id.as_deref() == Some(parent_session_id) {
        return Ok(());
    }
    Err(anyhow!(
        "CHILD_ADMISSION_PARENT_IDENTITY_MISMATCH:{}",
        child.session_id
    ))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ChildRuntimeRegistrationState {
    Absent,
    Unborn,
    Active,
    Terminal,
}

fn child_runtime_registration_state(
    request: &RegisterChildSessionRequest,
    expected_task_id: &str,
) -> Result<ChildRuntimeRegistrationState> {
    match session_log_contract::client::call_service(&SessionLogCommand::GetRuntimeLease(
        GetRuntimeLeaseRequest {
            runtime_id: request.child_runtime_id.clone(),
            database_path: None,
        },
    ))? {
        SessionLogResponse::RuntimeLeaseRead {
            runtime: Some(runtime),
        } => {
            validate_child_runtime_identity(&runtime, request, expected_task_id)?;
            classify_child_runtime_registration(&runtime, request)
        }
        SessionLogResponse::RuntimeLeaseRead { runtime: None } => {
            Ok(ChildRuntimeRegistrationState::Absent)
        }
        SessionLogResponse::Error { error } => Err(anyhow!(error)),
        other => Err(anyhow!(
            "unexpected session_db runtime read response for {}: {other:?}",
            request.child_runtime_id
        )),
    }
}

fn classify_child_runtime_registration(
    runtime: &RuntimeLeaseSnapshot,
    request: &RegisterChildSessionRequest,
) -> Result<ChildRuntimeRegistrationState> {
    if runtime.terminal {
        if runtime.lease_active
            || runtime.lease_id.as_deref() != Some(request.child_lease_id.as_str())
        {
            return Err(anyhow!(
                "CHILD_ADMISSION_RUNTIME_STATE_UNSETTLED:{}",
                request.child_runtime_id
            ));
        }
        return Ok(ChildRuntimeRegistrationState::Terminal);
    }
    if runtime.lease_active {
        if runtime.lease_id.as_deref() != Some(request.child_lease_id.as_str()) {
            return Err(anyhow!(
                "CHILD_ADMISSION_RUNTIME_IDENTITY_CONFLICT:{}",
                request.child_runtime_id
            ));
        }
        return Ok(ChildRuntimeRegistrationState::Active);
    }
    if runtime.lease_id.is_none()
        && runtime.runtime_state.is_none()
        && runtime.revision == 0
        && runtime.last_event_seq == 0
    {
        return Ok(ChildRuntimeRegistrationState::Unborn);
    }
    Err(anyhow!(
        "CHILD_ADMISSION_RUNTIME_STATE_UNSETTLED:{}",
        request.child_runtime_id
    ))
}

fn register_child_session_response(
    request: RegisterChildSessionRequest,
    outcome: RegisterChildSessionOutcome,
) -> Result<Value> {
    serde_json::to_value(RegisterChildSessionResponse {
        outcome,
        parent_session_id: request.parent_session_id,
        child_session_id: request.child_session_id,
        child_runtime_id: request.child_runtime_id,
        child_transaction_id: request.child_transaction_id,
        callback_request_id: request.callback_request_id,
        effect_id: request.effect_id,
        callback_delivery_route: request.callback_delivery_route,
    })
    .map_err(Into::into)
}

fn acknowledge_child_callback_from_store(
    store: &SessionLifecycleStore,
    request: AcknowledgeChildCallbackRequest,
) -> Result<Value> {
    let admission = store
        .child_admission(&request.child_session_id)?
        .ok_or_else(|| {
            anyhow!(
                "CHILD_CALLBACK_ACK_ADMISSION_NOT_FOUND:{}",
                request.child_session_id
            )
        })?;
    if admission.parent_session_id != request.parent_session_id
        || admission.parent_mission_revision_sha256 != request.parent_mission_revision_sha256
        || admission.commander_thread_id.as_deref() != Some(request.commander_thread_id.as_str())
        || admission.child_session_id != request.child_session_id
        || admission.child_runtime_id != request.child_runtime_id
        || admission.child_transaction_id != request.transaction_id
        || admission.child_lease_id != request.child_lease_id
        || admission.callback_request_id != request.transaction_id
        || admission.callback_delivery_route
            != Some(LifecycleCallbackDeliveryRoute::TrustedTuraDirectThreadWriter)
    {
        return Err(anyhow!(
            "CHILD_CALLBACK_ACK_ADMISSION_IDENTITY_MISMATCH:{}",
            request.child_session_id
        ));
    }

    let effect_identity = callback_effect_identity(&request.effect_identity);
    if let CallbackEffectIdentity::Exact { effect_id } = &effect_identity
        && admission.effect_id != *effect_id
    {
        return Err(anyhow!(
            "CHILD_CALLBACK_ACK_ADMISSION_EFFECT_IDENTITY_MISMATCH:{}",
            request.child_session_id
        ));
    }

    let callback = store
        .intaken_callback(&request.transaction_id, &request.event_id)?
        .ok_or_else(|| {
            anyhow!(
                "CHILD_CALLBACK_ACK_INTAKEN_CALLBACK_NOT_FOUND:{}:{}",
                request.transaction_id,
                request.event_id
            )
        })?;
    if callback.commander_session_id != request.parent_session_id
        || callback.parent_mission_revision_sha256 != request.parent_mission_revision_sha256
        || callback.commander_thread_id.as_deref() != Some(request.commander_thread_id.as_str())
        || callback.child_session_id != request.child_session_id
        || callback.runtime_id != request.child_runtime_id
        || callback.lease_id != request.child_lease_id
        || callback.transaction_id != request.transaction_id
        || callback.event_id != request.event_id
        || callback.callback_payload_sha256 != request.callback_payload_sha256
        || callback.effect_identity != effect_identity
        || callback.callback_delivery_route != admission.callback_delivery_route
    {
        return Err(anyhow!(
            "CHILD_CALLBACK_ACK_CALLBACK_IDENTITY_MISMATCH:{}:{}",
            request.transaction_id,
            request.event_id
        ));
    }
    if matches!(
        effect_identity,
        CallbackEffectIdentity::UnsettledEffect { .. }
    ) {
        return Err(anyhow!(
            "CALLBACK_UNSETTLED_EFFECT_ACK_BLOCKED:{}",
            request.event_id
        ));
    }

    let expected_continuation = ContinuationDispatchRecord::from_callback(&callback)?;
    let mut continuation = store
        .callback_continuation(&request.transaction_id, &request.event_id)?
        .ok_or_else(|| {
            anyhow!(
                "CHILD_CALLBACK_ACK_CONTINUATION_NOT_FOUND:{}:{}",
                request.transaction_id,
                request.event_id
            )
        })?;
    let commander_ack_confirms_delivery = continuation.state
        == ContinuationDispatchState::DeliveryUnsettled
        || (continuation.state == ContinuationDispatchState::Dispatched
            && continuation
                .direct_delivery_result
                .as_ref()
                .is_some_and(|result| result.kind == DirectDeliveryResultKind::Accepted));
    if commander_ack_confirms_delivery {
        let confirmation = json!({
            "schema_version": "tura_commander_ack_delivery_confirmation_v1",
            "confirmation": "exact_commander_callback_ack",
            "request_id": &continuation.request_id,
            "commander_session_id": &request.parent_session_id,
            "parent_mission_revision_sha256": &request.parent_mission_revision_sha256,
            "commander_thread_id": &request.commander_thread_id,
            "child_session_id": &request.child_session_id,
            "child_runtime_id": &request.child_runtime_id,
            "child_lease_id": &request.child_lease_id,
            "transaction_id": &request.transaction_id,
            "event_id": &request.event_id,
            "callback_payload_sha256": &request.callback_payload_sha256,
            "effect_identity": &request.effect_identity,
            "direct_delivery_call_id": &continuation.direct_delivery_call_id,
            "delivered_message_sha256": &continuation.delivered_message_sha256,
            "accepted_delivery_evidence_sha256": continuation
                .direct_delivery_result
                .as_ref()
                .map(|result| &result.evidence_sha256),
        });
        store.mark_callback_continuation_delivery_reconciled(&continuation, &confirmation)?;
        continuation = store
            .callback_continuation(&request.transaction_id, &request.event_id)?
            .ok_or_else(|| {
                anyhow!(
                    "CHILD_CALLBACK_ACK_CONTINUATION_NOT_FOUND:{}:{}",
                    request.transaction_id,
                    request.event_id
                )
            })?;
    } else {
        store.prepare_callback_continuation(&expected_continuation)?;
    }
    let reconciled = continuation
        .direct_delivery_result
        .as_ref()
        .is_some_and(|result| result.kind == DirectDeliveryResultKind::Reconciled);
    if !reconciled
        || !matches!(
            continuation.state,
            ContinuationDispatchState::Dispatched
                | ContinuationDispatchState::Completed
                | ContinuationDispatchState::Acknowledged
        )
    {
        return Err(anyhow!(
            "CHILD_CALLBACK_ACK_RECONCILED_CONTINUATION_REQUIRED:{}:{}",
            request.transaction_id,
            request.event_id
        ));
    }
    let outcome = match complete_and_ack_callback_continuation(store, &continuation)? {
        ContinuationWriteOutcome::Acknowledged => AcknowledgeChildCallbackOutcome::Acknowledged,
        ContinuationWriteOutcome::AlreadyAcknowledged => {
            AcknowledgeChildCallbackOutcome::AlreadyAcknowledged
        }
        other => {
            return Err(anyhow!(
                "CHILD_CALLBACK_ACK_LINEARIZATION_OUTCOME_INVALID:{}:{other:?}",
                continuation.request_id
            ));
        }
    };
    serde_json::to_value(AcknowledgeChildCallbackResponse {
        outcome,
        parent_session_id: request.parent_session_id,
        parent_mission_revision_sha256: request.parent_mission_revision_sha256,
        commander_thread_id: request.commander_thread_id,
        child_session_id: request.child_session_id,
        child_runtime_id: request.child_runtime_id,
        child_lease_id: request.child_lease_id,
        transaction_id: request.transaction_id,
        event_id: request.event_id,
        callback_payload_sha256: request.callback_payload_sha256,
        effect_identity: request.effect_identity,
    })
    .map_err(Into::into)
}

fn callback_effect_identity(
    identity: &AcknowledgeChildCallbackEffectIdentity,
) -> CallbackEffectIdentity {
    match identity {
        AcknowledgeChildCallbackEffectIdentity::Exact { effect_id } => {
            CallbackEffectIdentity::Exact {
                effect_id: effect_id.clone(),
            }
        }
        AcknowledgeChildCallbackEffectIdentity::ProvenZeroEffect {
            classification,
            evidence_sha256,
        } => CallbackEffectIdentity::ProvenZeroEffect {
            classification: classification.clone(),
            evidence_sha256: evidence_sha256.clone(),
        },
        AcknowledgeChildCallbackEffectIdentity::UnsettledEffect {
            classification,
            evidence_sha256,
        } => CallbackEffectIdentity::UnsettledEffect {
            classification: classification.clone(),
            evidence_sha256: evidence_sha256.clone(),
        },
    }
}

fn validate_child_runtime_identity(
    runtime: &RuntimeLeaseSnapshot,
    request: &RegisterChildSessionRequest,
    expected_task_id: &str,
) -> Result<()> {
    let lifecycle = runtime.lifecycle.as_ref();
    let exact = runtime.runtime_id == request.child_runtime_id
        && runtime.session_id == request.child_session_id
        && lifecycle.is_some_and(|identity| {
            identity.commander_session_id == request.parent_session_id
                && identity.transaction_id == request.child_transaction_id
                && identity.parent_mission_revision_sha256.as_deref()
                    == Some(request.parent_mission_revision_sha256.as_str())
                && identity.delegated_input_sha256.as_deref()
                    == Some(request.delegated_input_sha256.as_str())
                && identity.task_id.as_deref() == Some(expected_task_id)
                && identity.dispatch_runtime_id == request.child_runtime_id
                && identity.dispatch_lease_id == request.child_lease_id
        });
    if exact {
        Ok(())
    } else {
        Err(anyhow!(
            "CHILD_ADMISSION_RUNTIME_IDENTITY_CONFLICT:{}",
            request.child_runtime_id
        ))
    }
}

fn child_admission_record(
    request: &RegisterChildSessionRequest,
    ready_set_identity: ChildReadySetIdentity,
) -> ChildAdmissionRecord {
    ChildAdmissionRecord::new(
        &request.parent_session_id,
        &request.parent_mission_revision_sha256,
        request.commander_thread_id.clone(),
        &request.child_session_id,
        &request.child_runtime_id,
        &request.child_transaction_id,
        &request.child_lease_id,
        &request.callback_request_id,
        &request.effect_id,
        &request.delegated_input_sha256,
        canonical_value_sha256(&request.execution_payload),
        &request.session_directory,
        &request.session_name,
        request.created_at_ms,
    )
    .with_callback_delivery_route(callback_delivery_route(request.callback_delivery_route))
    .with_ready_set_identity(ready_set_identity)
}

fn callback_delivery_route(route: WireCallbackDeliveryRoute) -> LifecycleCallbackDeliveryRoute {
    match route {
        WireCallbackDeliveryRoute::TrustedTuraDirectThreadWriter => {
            LifecycleCallbackDeliveryRoute::TrustedTuraDirectThreadWriter
        }
    }
}

fn validate_existing_child_admission(
    admission: &ChildAdmissionRecord,
    request: &RegisterChildSessionRequest,
) -> Result<()> {
    if admission.ready_set_identity.is_none() {
        return Err(anyhow!("CHILD_READY_SET_LEGACY_ADMISSION_NOT_REPLAYABLE"));
    }
    if admission.callback_delivery_route.is_none() {
        return Err(anyhow!(
            "CHILD_DIRECT_WRITER_LEGACY_ADMISSION_NOT_REPLAYABLE"
        ));
    }
    if admission.parent_session_id != request.parent_session_id
        || admission.parent_mission_revision_sha256 != request.parent_mission_revision_sha256
        || admission.commander_thread_id != request.commander_thread_id
        || admission.callback_delivery_route
            != Some(callback_delivery_route(request.callback_delivery_route))
        || admission.child_session_id != request.child_session_id
        || admission.child_runtime_id != request.child_runtime_id
        || admission.child_transaction_id != request.child_transaction_id
        || admission.child_lease_id != request.child_lease_id
        || admission.callback_request_id != request.callback_request_id
        || admission.effect_id != request.effect_id
        || admission.delegated_input_sha256 != request.delegated_input_sha256
        || admission.execution_payload_sha256 != canonical_value_sha256(&request.execution_payload)
    {
        return Err(anyhow!(
            "CHILD_ADMISSION_IDENTITY_CONFLICT:{}",
            request.child_session_id
        ));
    }
    Ok(())
}

fn durable_child_admission_disposition_from_store(
    store: &SessionLifecycleStore,
    request: &RegisterChildSessionRequest,
) -> Result<DurableChildAdmissionDisposition> {
    let Some(admission) = store.child_admission(&request.child_session_id)? else {
        return Ok(DurableChildAdmissionDisposition::NotAdmitted);
    };
    validate_existing_child_admission(&admission, request)?;
    Ok(DurableChildAdmissionDisposition::Admitted)
}

fn child_completion_state(
    session_state: SessionState,
    task_plan: &TaskPlan,
) -> DurableChildCompletionState {
    if task_plan.detailed_tasks.is_empty() {
        return if session_state == SessionState::Completed {
            DurableChildCompletionState::SimpleEmptyPlanTerminal
        } else {
            DurableChildCompletionState::EmptyPlanPending
        };
    }
    if task_plan
        .detailed_tasks
        .iter()
        .all(|task| matches!(task.status, PlanStatus::Done | PlanStatus::Archived))
    {
        DurableChildCompletionState::TaskManagedTerminal
    } else {
        DurableChildCompletionState::TaskManagedPending
    }
}

fn admitted_pre_execution_failure_event_id(runtime_id: &str) -> String {
    format!("{runtime_id}:admitted-pre-execution-failure")
}

fn admitted_pre_execution_failure_receipt_from_store(
    store: &SessionLifecycleStore,
    request: &RegisterChildSessionRequest,
    task_id: &str,
) -> Result<Option<TerminalReceipt>> {
    let event_id = admitted_pre_execution_failure_event_id(&request.child_runtime_id);
    let receipt = match store.terminal_receipt(&request.child_transaction_id, &event_id) {
        Ok(receipt) => receipt,
        Err(error) if error.code == "TERMINAL_RECEIPT_NOT_FOUND" => return Ok(None),
        Err(error) => return Err(anyhow!(error.to_string())),
    };
    if receipt.commander_session_id != request.parent_session_id
        || receipt.child_session_id != request.child_session_id
        || receipt.runtime_id != request.child_runtime_id
        || receipt.lease_id != request.child_lease_id
        || receipt.terminal_state != TerminalState::Interrupted
        || receipt.task_id.as_deref() != Some(task_id)
        || receipt
            .audit_metadata
            .get("pre_execution_failure_classification")
            .and_then(Value::as_str)
            != Some("ADMITTED_PRE_EXECUTION_FAILURE")
    {
        return Err(anyhow!(
            "ADMITTED_PRE_EXECUTION_FAILURE_RECEIPT_IDENTITY_CONFLICT:{}",
            request.child_session_id
        ));
    }
    Ok(Some(receipt))
}

fn publish_admitted_pre_execution_failure_callback_from_store(
    store: &SessionLifecycleStore,
    request: &RegisterChildSessionRequest,
    task_id: &str,
    failure: Option<&anyhow::Error>,
) -> Result<(Value, TerminalDeliveryIdentity)> {
    let event_id = admitted_pre_execution_failure_event_id(&request.child_runtime_id);
    let receipt = match admitted_pre_execution_failure_receipt_from_store(store, request, task_id)?
    {
        Some(receipt) => receipt,
        None => {
            let failure = failure.ok_or_else(|| {
                anyhow!(
                    "ADMITTED_PRE_EXECUTION_FAILURE_RECEIPT_NOT_FOUND:{}",
                    request.child_session_id
                )
            })?;
            let finished_at_ms = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|duration| duration.as_millis().min(i64::MAX as u128) as i64)
                .unwrap_or_default();
            let mut receipt = TerminalReceipt::new(
                TerminalReceiptIdentity::new(
                    &request.child_transaction_id,
                    &event_id,
                    0,
                    &request.parent_session_id,
                    &request.child_session_id,
                    &request.child_runtime_id,
                    &request.child_lease_id,
                ),
                TerminalState::Interrupted,
                finished_at_ms,
            );
            receipt.task_id = Some(task_id.to_string());
            receipt.audit_metadata.insert(
                "parent_mission_revision_sha256".to_string(),
                json!(request.parent_mission_revision_sha256),
            );
            receipt.audit_metadata.insert(
                "delegated_input_sha256".to_string(),
                json!(request.delegated_input_sha256),
            );
            receipt.audit_metadata.insert(
                "dispatch_runtime_id".to_string(),
                json!(request.child_runtime_id),
            );
            receipt.audit_metadata.insert(
                "dispatch_lease_id".to_string(),
                json!(request.child_lease_id),
            );
            receipt
                .audit_metadata
                .insert("runtime_state".to_string(), Value::Null);
            receipt
                .audit_metadata
                .insert("runtime_expected_revision".to_string(), json!(0));
            receipt.audit_metadata.insert(
                "pre_execution_failure_classification".to_string(),
                json!("ADMITTED_PRE_EXECUTION_FAILURE"),
            );
            receipt.audit_metadata.insert(
                "pre_execution_failure_sha256".to_string(),
                json!(canonical_value_sha256(&json!(format!("{failure:#}")))),
            );
            store
                .write_terminal_receipt(&receipt)
                .map_err(|error| anyhow!(error.to_string()))?;
            receipt
        }
    };
    store
        .intake(&receipt.transaction_id, &receipt.event_id)
        .map_err(|error| anyhow!(error.to_string()))?;
    let delivery = TerminalDeliveryIdentity {
        commander_session_id: receipt.commander_session_id.clone(),
        transaction_id: receipt.transaction_id.clone(),
        event_id: receipt.event_id.clone(),
        runtime_id: receipt.runtime_id.clone(),
        callback_payload_sha256: None,
        callback_effect_identity: None,
    };
    publish_terminal_failure_callback_from_store(store, delivery)?
        .ok_or_else(|| anyhow!("ADMITTED_PRE_EXECUTION_FAILURE_CALLBACK_NOT_PUBLISHED"))
}

fn cancel_unstarted_child_session(request: &RegisterChildSessionRequest) -> Result<()> {
    let snapshot = read_session_snapshot(&request.child_session_id)?.ok_or_else(|| {
        anyhow!(
            "ADMITTED_PRE_EXECUTION_FAILURE_CHILD_SESSION_NOT_FOUND:{}",
            request.child_session_id
        )
    })?;
    ensure_child_parent_identity(&snapshot, &request.parent_session_id)?;
    match snapshot.lifecycle_projection.state {
        SessionState::Cancelled => {
            return require_admitted_pre_execution_child_terminal(request);
        }
        SessionState::Created | SessionState::Running
            if snapshot.lifecycle_projection.active_runtime_id.is_none() => {}
        state => {
            return Err(anyhow!(
                "ADMITTED_PRE_EXECUTION_FAILURE_CHILD_SESSION_NOT_UNSTARTED:{}:{state:?}",
                request.child_session_id
            ));
        }
    }
    match session_log_contract::client::call_service(&SessionLogCommand::ExecuteSessionCommand(
        ExecuteSessionCommandRequest {
            command_id: format!(
                "child-pre-execution-failure-{}",
                request.callback_request_id
            ),
            session_id: request.child_session_id.clone(),
            session_command: SessionCommand::ApplyRuntimeState {
                state: SessionState::Cancelled,
            },
            message_projection: None,
        },
    ))? {
        SessionLogResponse::SessionCommandApplied { result }
            if result.projection.state == SessionState::Cancelled =>
        {
            require_admitted_pre_execution_child_terminal(request)
        }
        SessionLogResponse::Error { error } => Err(anyhow!(error)),
        other => Err(anyhow!(
            "ADMITTED_PRE_EXECUTION_FAILURE_CHILD_TERMINALIZATION_UNEXPECTED:{other:?}"
        )),
    }
}

fn require_admitted_pre_execution_child_terminal(
    request: &RegisterChildSessionRequest,
) -> Result<()> {
    let snapshot = read_session_snapshot(&request.child_session_id)?.ok_or_else(|| {
        anyhow!(
            "ADMITTED_PRE_EXECUTION_FAILURE_CHILD_TERMINAL_READBACK_MISSING:{}",
            request.child_session_id
        )
    })?;
    ensure_child_parent_identity(&snapshot, &request.parent_session_id)?;
    if snapshot.lifecycle_projection.state != SessionState::Cancelled
        || snapshot.lifecycle_projection.active_runtime_id.is_some()
    {
        return Err(anyhow!(
            "ADMITTED_PRE_EXECUTION_FAILURE_CHILD_TERMINAL_READBACK_MISMATCH:{}:{:?}",
            request.child_session_id,
            snapshot.lifecycle_projection.state
        ));
    }
    Ok(())
}

fn child_execution_payload(request: &RegisterChildSessionRequest) -> Result<Value> {
    let mut payload = request.execution_payload.clone();
    let object = payload
        .as_object_mut()
        .ok_or_else(|| anyhow!("CHILD_ADMISSION_PAYLOAD_INVALID:execution_payload"))?;
    object.insert(
        "parent_session_id".to_string(),
        Value::String(request.parent_session_id.clone()),
    );
    object.insert(
        "parent_mission_revision_sha256".to_string(),
        Value::String(request.parent_mission_revision_sha256.clone()),
    );
    object.insert(
        "delegated_input_sha256".to_string(),
        Value::String(request.delegated_input_sha256.clone()),
    );
    object.insert(
        "lifecycle".to_string(),
        json!({
            "transaction_id": request.child_transaction_id,
            "commander_session_id": request.parent_session_id,
            "parent_mission_revision_sha256": request.parent_mission_revision_sha256,
            "delegated_input_sha256": request.delegated_input_sha256,
            "task_id": null,
            "goal_id": null,
            "operator_override": false,
        }),
    );
    Ok(payload)
}

fn bind_child_task_identity(payload: &mut Value, task_id: &str) -> Result<()> {
    let object = payload
        .as_object_mut()
        .ok_or_else(|| anyhow!("CHILD_ADMISSION_PAYLOAD_INVALID:execution_payload"))?;
    if let Some(existing) = object.get("task_id").and_then(Value::as_str)
        && existing != task_id
    {
        return Err(anyhow!("CHILD_READY_SET_TASK_ID_CONFLICT"));
    }
    object.insert("task_id".to_string(), Value::String(task_id.to_string()));
    let lifecycle = object
        .get_mut("lifecycle")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| anyhow!("CHILD_ADMISSION_PAYLOAD_INVALID:lifecycle"))?;
    if let Some(existing) = lifecycle.get("task_id").and_then(Value::as_str)
        && existing != task_id
    {
        return Err(anyhow!("CHILD_READY_SET_TASK_ID_CONFLICT"));
    }
    lifecycle.insert("task_id".to_string(), Value::String(task_id.to_string()));
    Ok(())
}

fn validated_child_execution_payload(request: &RegisterChildSessionRequest) -> Result<Value> {
    let payload = child_execution_payload(request)?;
    let enqueue = EnqueueTurnRequest {
        runtime_id: request.child_runtime_id.clone(),
        session_id: request.child_session_id.clone(),
        payload: payload.clone(),
    };
    let mut run_request = payload_to_run_agent_request(&enqueue, &request.child_lease_id, None)?;
    run_request
        .validate_delegated_identity()
        .map_err(anyhow::Error::msg)?;
    validate_delegated_input_digest(&mut run_request, true)?;

    if run_request.runtime_context.is_some() && run_request.task_context_capsule.is_some() {
        return Err(anyhow!(
            "TASK_CONTEXT_CAPSULE_CONFLICT: runtime_context and task_context_capsule are mutually exclusive"
        ));
    }
    if let Some(value) = run_request.task_context_capsule.as_ref() {
        let capsule = TaskContextCapsule::from_value(value.clone()).map_err(anyhow::Error::msg)?;
        capsule
            .bind_jspace(run_request.jspace_contract.as_ref())
            .map_err(anyhow::Error::msg)?;
    }

    Ok(payload)
}

fn validate_prepared_parent_task_plan_identity(expected: Option<&str>, actual: &str) -> Result<()> {
    if let Some(expected) = expected
        && expected != actual
    {
        return Err(anyhow::Error::new(PreClaimChildAdmissionError {
            message: format!(
                "TASK_PACKET_PREPARED_PARENT_TASK_PLAN_DRIFT:expected={expected},actual={actual}"
            ),
        }));
    }
    Ok(())
}

fn validate_child_ready_set_binding(
    service: &ExecutionService,
    parent: &SessionSnapshot,
    request: &RegisterChildSessionRequest,
    payload: &Value,
    expected_parent_task_plan_sha256: Option<&str>,
) -> Result<ChildReadySetIdentity> {
    let preclaim_task_plan = &parent.lifecycle_projection.task_plan;
    let mut durable_claim_already_exists = false;
    let prepared = (|| -> Result<PreparedChildReadySet> {
        let enqueue = EnqueueTurnRequest {
            runtime_id: request.child_runtime_id.clone(),
            session_id: request.child_session_id.clone(),
            payload: payload.clone(),
        };
        let run_request = payload_to_run_agent_request(&enqueue, &request.child_lease_id, None)?;
        let capsule_value = run_request
            .task_context_capsule
            .as_ref()
            .ok_or_else(|| anyhow!("CHILD_READY_SET_TASK_CONTEXT_CAPSULE_MISSING"))?;
        let capsule =
            TaskContextCapsule::from_value(capsule_value.clone()).map_err(anyhow::Error::msg)?;
        let task_id = capsule
            .mission
            .task_id
            .as_deref()
            .ok_or_else(|| anyhow!("CHILD_READY_SET_CAPSULE_TASK_ID_MISSING"))?;
        if run_request
            .task_id
            .as_deref()
            .is_some_and(|candidate| candidate != task_id)
            || run_request
                .lifecycle
                .as_ref()
                .and_then(|lifecycle| lifecycle.task_id.as_deref())
                .is_some_and(|candidate| candidate != task_id)
        {
            return Err(anyhow!("CHILD_READY_SET_TASK_ID_CONFLICT"));
        }
        let task = preclaim_task_plan
            .detailed_tasks
            .iter()
            .find(|task| task.task_id == task_id)
            .ok_or_else(|| anyhow!("CHILD_READY_SET_TASK_NOT_FOUND:{task_id}"))?;
        let contract = task
            .scheduling_contract
            .as_ref()
            .cloned()
            .ok_or_else(|| anyhow!("CHILD_READY_SET_SCHEDULING_CONTRACT_MISSING:{task_id}"))?;
        contract
            .validate(task_id)
            .map_err(|reason| anyhow!(reason))?;
        let task_scheduling_contract_sha256 = contract.contract_sha256();
        if capsule.mission.mission_id != contract.mission_id {
            return Err(anyhow!("CHILD_READY_SET_MISSION_ID_MISMATCH"));
        }
        let computed_semantic_dispatch_key = contract.semantic_dispatch_sha256(task_id);
        if contract.semantic_dispatch_key != computed_semantic_dispatch_key {
            return Err(anyhow!("CHILD_READY_SET_SEMANTIC_DISPATCH_KEY_MISMATCH"));
        }
        if request.parent_mission_revision_sha256 != contract.authority_mission_revision_sha256 {
            return Err(anyhow!("CHILD_READY_SET_AUTHORITY_REVISION_MISMATCH"));
        }
        if request.delegated_input_sha256 != contract.delegated_input_sha256 {
            return Err(anyhow!("CHILD_READY_SET_DELEGATED_INPUT_IDENTITY_MISMATCH"));
        }

        let existing_claim = task.dispatch_claim.as_ref();
        durable_claim_already_exists = existing_claim.is_some();
        let computed_scope_claim_sha256 = scope_claim_sha256(&contract);
        let parent_task_plan_sha256 = if let Some(claim) = existing_claim {
            validate_dispatch_claim_request(
                claim,
                request,
                task_id,
                &contract,
                &task_scheduling_contract_sha256,
                &computed_scope_claim_sha256,
                &capsule.semantic_sha256,
            )?;
            if task.status != lifecycle::PlanStatus::Doing
                || task.sub_session_id != request.child_session_id
            {
                return Err(anyhow!("CHILD_READY_SET_DURABLE_CLAIM_STATE_MISMATCH"));
            }
            claim.parent_task_plan_sha256.clone()
        } else {
            task_plan_sha256(preclaim_task_plan)?
        };
        validate_prepared_parent_task_plan_identity(
            expected_parent_task_plan_sha256,
            &parent_task_plan_sha256,
        )?;
        if capsule.semantic_sha256 != contract.task_context_capsule_semantic_sha256 {
            return Err(anyhow!(
                "CHILD_READY_SET_CAPSULE_SEMANTIC_IDENTITY_MISMATCH"
            ));
        }
        let mut exact_input_sha256s = Vec::new();
        for reference in &capsule.evidence_refs {
            let sha256 = reference.sha256.as_ref().ok_or_else(|| {
                anyhow!(
                    "CHILD_READY_SET_EXACT_INPUT_IDENTITY_MISSING:{}",
                    reference.id
                )
            })?;
            exact_input_sha256s.push(sha256.clone());
        }
        exact_input_sha256s.sort();
        exact_input_sha256s.dedup();
        if exact_input_sha256s != contract.exact_input_sha256s {
            return Err(anyhow!("CHILD_READY_SET_EXACT_INPUT_IDENTITY_MISMATCH"));
        }

        let jspace_contract = run_request
            .jspace_contract
            .as_ref()
            .ok_or_else(|| anyhow!("CHILD_READY_SET_JSPACE_CONTRACT_MISSING"))?;
        let matcher = JSpaceMatcher::from_value(Path::new(&parent.workspace), jspace_contract)
            .map_err(|error| anyhow!("{}:{}", error.code(), error))?;
        if matcher.authorization_semantic_sha256() != contract.jspace_authorization_semantic_sha256
            || matcher.scope_projection().read_scopes != contract.read_scopes
            || matcher.scope_projection().write_scopes != contract.write_scopes
        {
            return Err(anyhow!("CHILD_READY_SET_JSPACE_SCOPE_IDENTITY_MISMATCH"));
        }
        if matcher.declared_targets() != contract.declared_targets {
            return Err(anyhow!("CHILD_READY_SET_JSPACE_TARGET_IDENTITY_MISMATCH"));
        }
        let effective_parallel_workers =
            maximum_parallel_runtime_workers(run_request.maximum_parallel_runtime_workers);
        if effective_parallel_workers != contract.maximum_parallel_runtime_workers {
            return Err(anyhow!("CHILD_READY_SET_PARALLEL_RUNTIME_LIMIT_MISMATCH"));
        }
        let exact_input_set = exact_input_sha256s.into_iter().collect::<BTreeSet<_>>();
        let active_leases = service.sessions.lock().clone();
        let (mut evidence, active_identities) = collect_task_ready_set_evidence(
            &request.parent_session_id,
            preclaim_task_plan,
            &request.parent_mission_revision_sha256,
            Some(request.child_lease_id.clone()),
            Some(exact_input_set.clone()),
            existing_claim.map(|_| task_id.to_string()),
            parent
                .lifecycle_projection
                .state
                .can_transition_to(lifecycle::SessionState::Running),
            service.runtime_slots.active_count(),
            &active_leases,
        );
        evidence.semantic_dispatch_identity_valid = true;
        evidence.parallel_runtime_limit_supported = true;
        populate_scope_evidence(&contract, &mut evidence, &active_identities);
        let decision = classify_task_ready_set(preclaim_task_plan, task_id, &evidence);
        if decision.state != TaskReadySetState::Ready {
            return Err(anyhow!(
                "CHILD_READY_SET_BLOCKED:{:?}:{}",
                decision.state,
                decision.reason_codes.join(",")
            ));
        }

        let claim = TaskDispatchClaimV1 {
            schema_version: lifecycle::TASK_DISPATCH_CLAIM_SCHEMA_VERSION.to_string(),
            mission_id: contract.mission_id.clone(),
            task_id: task_id.to_string(),
            child_session_id: request.child_session_id.clone(),
            child_runtime_id: request.child_runtime_id.clone(),
            child_lease_id: request.child_lease_id.clone(),
            child_transaction_id: request.child_transaction_id.clone(),
            semantic_dispatch_key: contract.semantic_dispatch_key.clone(),
            authority_mission_revision_sha256: request.parent_mission_revision_sha256.clone(),
            delegated_input_sha256: request.delegated_input_sha256.clone(),
            task_context_capsule_semantic_sha256: capsule.semantic_sha256.clone(),
            parent_task_plan_sha256: parent_task_plan_sha256.clone(),
            task_scheduling_contract_sha256: task_scheduling_contract_sha256.clone(),
            scope_claim_sha256: computed_scope_claim_sha256.clone(),
        };
        Ok(PreparedChildReadySet {
            task_id: task_id.to_string(),
            contract,
            existing_claim: existing_claim.is_some(),
            parent_task_plan_sha256,
            task_scheduling_contract_sha256,
            computed_scope_claim_sha256,
            capsule_semantic_sha256: capsule.semantic_sha256,
            claim,
        })
    })()
    .map_err(|error| {
        if durable_claim_already_exists
            || error
                .downcast_ref::<PreClaimChildAdmissionError>()
                .is_some()
        {
            error
        } else {
            anyhow::Error::new(PreClaimChildAdmissionError {
                message: error.to_string(),
            })
        }
    })?;
    let PreparedChildReadySet {
        task_id,
        contract,
        existing_claim,
        parent_task_plan_sha256,
        task_scheduling_contract_sha256,
        computed_scope_claim_sha256,
        capsule_semantic_sha256,
        claim,
    } = prepared;
    let claimed_plan = if !existing_claim {
        let response = session_log_contract::client::call_service(
            &SessionLogCommand::ExecuteSessionCommand(ExecuteSessionCommandRequest {
                command_id: format!("ready-set-claim:{}", request.child_transaction_id),
                session_id: request.parent_session_id.clone(),
                session_command: SessionCommand::ClaimScheduledChildTask {
                    expected_task_plan: preclaim_task_plan.clone(),
                    claim: claim.clone(),
                    now: chrono::Utc::now(),
                },
                message_projection: None,
            }),
        )?;
        match response {
            SessionLogResponse::SessionCommandApplied { result }
                if matches!(
                    result.event,
                    lifecycle::SessionEvent::ScheduledChildTaskClaimed {
                        claim: ref event_claim,
                        ..
                    } if event_claim == &claim
                ) =>
            {
                result.projection.task_plan
            }
            SessionLogResponse::Error { error } => return Err(anyhow!(error)),
            other => {
                return Err(anyhow!("CHILD_READY_SET_DURABLE_CLAIM_FAILED:{other:?}"));
            }
        }
    } else {
        preclaim_task_plan.clone()
    };
    let claimed_task = claimed_plan
        .detailed_tasks
        .iter()
        .find(|task| task.task_id == task_id)
        .ok_or_else(|| anyhow!("CHILD_READY_SET_TASK_NOT_FOUND:claim"))?;
    let stored_claim = claimed_task
        .dispatch_claim
        .as_ref()
        .ok_or_else(|| anyhow!("CHILD_READY_SET_DURABLE_CLAIM_MISSING"))?;
    validate_dispatch_claim_request(
        stored_claim,
        request,
        &task_id,
        &contract,
        &task_scheduling_contract_sha256,
        &computed_scope_claim_sha256,
        &capsule_semantic_sha256,
    )?;
    if stored_claim != &claim
        || claimed_task.status != lifecycle::PlanStatus::Doing
        || claimed_task.sub_session_id != request.child_session_id
    {
        return Err(anyhow!("CHILD_READY_SET_DURABLE_CLAIM_IDENTITY_MISMATCH"));
    }

    Ok(ChildReadySetIdentity::new(
        &contract.mission_id,
        &task_id,
        &contract.semantic_dispatch_key,
        parent_task_plan_sha256,
        task_scheduling_contract_sha256,
        computed_scope_claim_sha256,
        &capsule_semantic_sha256,
        contract.read_scopes.clone(),
        contract.write_scopes.clone(),
        contract.conflict_identities.clone(),
    ))
}

fn validate_dispatch_claim_request(
    claim: &TaskDispatchClaimV1,
    request: &RegisterChildSessionRequest,
    task_id: &str,
    contract: &TaskSchedulingContractV1,
    task_scheduling_contract_sha256: &str,
    scope_claim_sha256: &str,
    task_context_capsule_semantic_sha256: &str,
) -> Result<()> {
    claim.validate().map_err(anyhow::Error::msg)?;
    if claim.mission_id != contract.mission_id
        || claim.task_id != task_id
        || claim.child_session_id != request.child_session_id
        || claim.child_runtime_id != request.child_runtime_id
        || claim.child_lease_id != request.child_lease_id
        || claim.child_transaction_id != request.child_transaction_id
        || claim.semantic_dispatch_key != contract.semantic_dispatch_key
        || claim.authority_mission_revision_sha256 != request.parent_mission_revision_sha256
        || claim.delegated_input_sha256 != request.delegated_input_sha256
        || claim.task_context_capsule_semantic_sha256 != task_context_capsule_semantic_sha256
        || claim.task_scheduling_contract_sha256 != task_scheduling_contract_sha256
        || claim.scope_claim_sha256 != scope_claim_sha256
    {
        return Err(anyhow!("CHILD_READY_SET_DURABLE_CLAIM_REQUEST_MISMATCH"));
    }
    Ok(())
}

fn validate_terminalization_identity(
    snapshot: &RuntimeLeaseSnapshot,
    lease: &RuntimeLease,
    session_id: &str,
    runtime_id: &str,
) -> Result<()> {
    if snapshot.runtime_id != runtime_id
        || snapshot.session_id != session_id
        || snapshot.lease_id.as_deref() != Some(lease.lease_id.as_str())
    {
        return Err(anyhow!(
            "RUNTIME_TERMINALIZATION_DURABLE_IDENTITY_MISMATCH:session={session_id},runtime={runtime_id},snapshot_session={},snapshot_runtime={},snapshot_lease={:?},router_lease={}",
            snapshot.session_id,
            snapshot.runtime_id,
            snapshot.lease_id,
            lease.lease_id
        ));
    }
    if snapshot.database_path.trim().is_empty() {
        return Err(anyhow!(
            "RUNTIME_TERMINALIZATION_DATABASE_PATH_MISSING:{runtime_id}"
        ));
    }
    Ok(())
}

fn lifecycle_store(commander_session_id: &str) -> Result<SessionLifecycleStore> {
    let base = session_log_contract::client::default_db_dir().join("session_lifecycle_v1");
    let root = commander_store_path(&base, commander_session_id)?;
    Ok(SessionLifecycleStore::open(
        root,
        commander_session_id,
        LifecycleConfig::default(),
    )?)
}

fn lifecycle_store_if_exists(commander_session_id: &str) -> Result<Option<SessionLifecycleStore>> {
    let base = session_log_contract::client::default_db_dir().join("session_lifecycle_v1");
    let root = commander_store_path(&base, commander_session_id)?;
    Ok(SessionLifecycleStore::open_existing(
        root,
        commander_session_id,
        LifecycleConfig::default(),
    )?)
}

fn terminal_state(state: SessionState) -> Result<TerminalState> {
    match state {
        SessionState::Completed => Ok(TerminalState::Completed),
        SessionState::Failed => Ok(TerminalState::Failed),
        SessionState::Cancelled => Ok(TerminalState::Cancelled),
        SessionState::Interrupted => Ok(TerminalState::Interrupted),
        other => Err(anyhow!("SESSION_STATE_NOT_TERMINAL:{other:?}")),
    }
}

fn runtime_terminal_state(state: RuntimeState) -> Result<TerminalState> {
    match state {
        RuntimeState::Finished => Ok(TerminalState::Completed),
        RuntimeState::Failed | RuntimeState::TimedOut => Ok(TerminalState::Failed),
        RuntimeState::Cancelled => Ok(TerminalState::Cancelled),
        other => Err(anyhow!("RUNTIME_STATE_NOT_TERMINAL:{other:?}")),
    }
}

fn recovery_terminal_state(recovery: &RuntimeRecoveryReceipt) -> TerminalState {
    match recovery.reason {
        RecoveryCloseRuntimeReason::OrphanedRuntime => TerminalState::Cancelled,
        RecoveryCloseRuntimeReason::UnbornRuntime => TerminalState::Interrupted,
        RecoveryCloseRuntimeReason::CommanderConvergenceProven => match recovery.session_state {
            SessionState::Completed => TerminalState::Completed,
            _ => TerminalState::Failed,
        },
    }
}

fn recovery_runtime_state(recovery: &RuntimeRecoveryReceipt) -> Value {
    match recovery.reason {
        RecoveryCloseRuntimeReason::OrphanedRuntime => json!(RuntimeState::Cancelled),
        RecoveryCloseRuntimeReason::UnbornRuntime => Value::Null,
        RecoveryCloseRuntimeReason::CommanderConvergenceProven => match recovery.session_state {
            SessionState::Completed => json!(RuntimeState::Finished),
            _ => json!(RuntimeState::Failed),
        },
    }
}

fn register_and_activate_runtime(
    session_id: &str,
    runtime_id: &str,
    lease_id: &str,
    fallback_from_id: Option<String>,
    lifecycle: Option<RuntimeLifecycleIdentity>,
) -> Result<()> {
    let response = session_log_contract::client::call_service(
        &SessionLogCommand::RegisterRuntime(RegisterRuntimeRequest {
            runtime_id: runtime_id.to_string(),
            session_id: session_id.to_string(),
            fallback_from_id,
            lifecycle,
        }),
    )?;
    match response {
        SessionLogResponse::RuntimeRegistered {
            result:
                RuntimeRegistrationOutcome::Registered { .. }
                | RuntimeRegistrationOutcome::AlreadyRegistered { .. },
        } => {}
        SessionLogResponse::RuntimeRegistered { result } => {
            return Err(anyhow!(
                "session_db rejected runtime {runtime_id} registration for session {session_id}: {result:?}"
            ));
        }
        SessionLogResponse::Error { error } => {
            return Err(anyhow!(
                "session_db failed runtime {runtime_id} registration for session {session_id}: {error}"
            ));
        }
        other => {
            return Err(anyhow!(
                "unexpected session_db registration response for runtime {runtime_id}: {other:?}"
            ));
        }
    }

    let response = session_log_contract::client::call_service(
        &SessionLogCommand::ActivateRuntimeLease(ActivateRuntimeLeaseRequest {
            runtime_id: runtime_id.to_string(),
            lease_id: lease_id.to_string(),
        }),
    )?;
    match response {
        SessionLogResponse::RuntimeLeaseActivated {
            result: RuntimeLeaseOutcome::Activated | RuntimeLeaseOutcome::AlreadyActive,
        } => Ok(()),
        SessionLogResponse::RuntimeLeaseActivated { result } => Err(anyhow!(
            "session_db rejected lease {lease_id} for runtime {runtime_id}: {result:?}"
        )),
        SessionLogResponse::Error { error } => Err(anyhow!(
            "session_db failed to activate lease for runtime {runtime_id}: {error}"
        )),
        other => Err(anyhow!(
            "unexpected session_db lease response for runtime {runtime_id}: {other:?}"
        )),
    }
}

fn runtime_registration_fallback(
    session_id: &str,
    expected_user_input: Option<&str>,
) -> Result<Option<String>> {
    let response = session_log_contract::client::call_service(&SessionLogCommand::GetSession(
        GetSessionRequest {
            session_id: session_id.to_string(),
        },
    ))?;
    match response {
        SessionLogResponse::Session {
            session: Some(session),
        } => {
            let Some(latest_runtime_id) =
                failed_session_runtime_fallback(&session.lifecycle_projection)?
            else {
                return Ok(None);
            };
            let expected_user_input = expected_user_input
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| {
                    anyhow!(
                        "FAILED_SESSION_RETRY_INPUT_MISSING:{}",
                        session.lifecycle_projection.session_id
                    )
                })?;
            failed_session_retry_root(
                &session.lifecycle_projection,
                &latest_runtime_id,
                expected_user_input,
                replay_runtime_identity,
            )
            .map(Some)
        }
        SessionLogResponse::Session { session: None } => Err(anyhow!(
            "session_db cannot register runtime for missing session {session_id}"
        )),
        SessionLogResponse::Error { error } => Err(anyhow!(
            "session_db failed to read session {session_id} before runtime registration: {error}"
        )),
        other => Err(anyhow!(
            "unexpected session_db response while reading session {session_id}: {other:?}"
        )),
    }
}

fn validate_continuation_fallback_source(session_id: &str, fallback_from_id: &str) -> Result<()> {
    let response = session_log_contract::client::call_service(&SessionLogCommand::GetSession(
        GetSessionRequest {
            session_id: session_id.to_string(),
        },
    ))?;
    match response {
        SessionLogResponse::Session {
            session: Some(session),
        } if session.lifecycle_projection.state == SessionState::Failed
            && session.lifecycle_projection.active_runtime_id.is_none()
            && session
                .lifecycle_projection
                .runtime_ids
                .last()
                .map(String::as_str)
                == Some(fallback_from_id) =>
        {
            Ok(())
        }
        SessionLogResponse::Session {
            session: Some(session),
        } => Err(anyhow!(
            "CALLBACK_CONTINUATION_FALLBACK_SOURCE_NOT_LATEST_FAILED:{}:{}:{:?}:{:?}",
            session_id,
            fallback_from_id,
            session.lifecycle_projection.state,
            session.lifecycle_projection.runtime_ids.last()
        )),
        SessionLogResponse::Session { session: None } => Err(anyhow!(
            "CALLBACK_CONTINUATION_FALLBACK_SESSION_NOT_FOUND:{}",
            session_id
        )),
        SessionLogResponse::Error { error } => Err(anyhow!(error)),
        other => Err(anyhow!(
            "unexpected session_db response while validating callback fallback: {other:?}"
        )),
    }
}

const MAX_FAILED_RUNTIME_RECONCILIATION_DEPTH: usize = 64;

#[derive(Debug, Clone, PartialEq, Eq)]
struct RetryRuntimeIdentity {
    runtime_id: String,
    session_id: String,
    state: RuntimeState,
    latest_user_input: Option<String>,
}

fn replay_runtime_identity(runtime_id: &str) -> Result<Option<RetryRuntimeIdentity>> {
    let response = session_log_contract::client::call_service(&SessionLogCommand::ReplayRuntime(
        ReplayRuntimeRequest {
            runtime_id: runtime_id.to_string(),
        },
    ))?;
    match response {
        SessionLogResponse::RuntimeReplayed {
            runtime: Some(runtime),
        } => Ok(Some(retry_runtime_identity(&runtime.aggregate))),
        SessionLogResponse::RuntimeReplayed { runtime: None } => Ok(None),
        SessionLogResponse::Error { error } => Err(anyhow!(
            "session_db failed to replay runtime {runtime_id}: {error}"
        )),
        other => Err(anyhow!(
            "unexpected session_db response while replaying runtime {runtime_id}: {other:?}"
        )),
    }
}

fn retry_runtime_identity(runtime: &RuntimeAggregate) -> RetryRuntimeIdentity {
    let latest_user_input = runtime
        .input
        .as_ref()
        .and_then(|input| input.get("messages"))
        .and_then(Value::as_array)
        .and_then(|messages| {
            messages.iter().rev().find_map(|message| {
                (message.get("role").and_then(Value::as_str) == Some("user"))
                    .then(|| message.get("content").and_then(Value::as_str))
                    .flatten()
                    .map(str::to_string)
            })
        });
    RetryRuntimeIdentity {
        runtime_id: runtime.runtime_id.clone(),
        session_id: runtime.session_id.clone(),
        state: runtime.state,
        latest_user_input,
    }
}

fn failed_session_retry_root<F>(
    projection: &lifecycle::SessionProjection,
    latest_runtime_id: &str,
    expected_user_input: &str,
    mut replay_runtime: F,
) -> Result<String>
where
    F: FnMut(&str) -> Result<Option<RetryRuntimeIdentity>>,
{
    let latest_index = projection
        .runtime_ids
        .iter()
        .position(|runtime_id| runtime_id == latest_runtime_id)
        .ok_or_else(|| {
            anyhow!(
                "FAILED_SESSION_RETRY_SOURCE_NOT_IN_PROJECTION:{}:{}",
                projection.session_id,
                latest_runtime_id
            )
        })?;
    let mut root = None;
    for runtime_id in projection.runtime_ids[..=latest_index]
        .iter()
        .rev()
        .take(MAX_FAILED_RUNTIME_RECONCILIATION_DEPTH)
    {
        let identity = replay_runtime(runtime_id)?.ok_or_else(|| {
            anyhow!(
                "FAILED_SESSION_RETRY_SOURCE_MISSING:{}:{}",
                projection.session_id,
                runtime_id
            )
        })?;
        if identity.runtime_id != *runtime_id || identity.session_id != projection.session_id {
            return Err(anyhow!(
                "FAILED_SESSION_RETRY_SOURCE_IDENTITY_MISMATCH:{}:{}",
                projection.session_id,
                runtime_id
            ));
        }
        if !matches!(
            identity.state,
            RuntimeState::Failed | RuntimeState::TimedOut
        ) {
            break;
        }
        root = Some(identity);
    }
    let root = root.ok_or_else(|| {
        anyhow!(
            "FAILED_SESSION_RETRY_ROOT_NOT_FOUND:{}:{}",
            projection.session_id,
            latest_runtime_id
        )
    })?;
    if root.latest_user_input.as_deref().map(str::trim) != Some(expected_user_input.trim()) {
        return Err(anyhow!(
            "FAILED_SESSION_RETRY_ROOT_INPUT_MISMATCH:{}:{}",
            projection.session_id,
            root.runtime_id
        ));
    }
    Ok(root.runtime_id)
}

fn failed_session_runtime_fallback(
    projection: &lifecycle::SessionProjection,
) -> Result<Option<String>> {
    if projection.state != SessionState::Failed {
        return Ok(None);
    }
    projection
        .runtime_ids
        .last()
        .cloned()
        .map(Some)
        .ok_or_else(|| {
            anyhow!(
                "FAILED_SESSION_MISSING_RUNTIME_LINEAGE:{}",
                projection.session_id
            )
        })
}

fn debug_runtime_enabled() -> bool {
    std::env::var("TURA_DEBUG_RUNTIME")
        .ok()
        .is_some_and(|value| {
            matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
}

#[cfg(test)]
mod tests {
    use super::{
        ChildRuntimeRegistrationState, CommanderCompileParent, DirectDeliveryAdmission,
        DurableChildAdmissionDisposition, DurableChildCompletionState, EnqueueTurnRequest,
        ExecutionService, RetryRuntimeIdentity, RouterRecoveryCloseRuntimeRequest, RuntimeLease,
        TerminalDeliveryIdentity, WireCallbackDeliveryRoute, acknowledge_child_callback_from_store,
        admit_trusted_direct_thread_delivery, admitted_pre_execution_failure_receipt_from_store,
        callback_continuation_payload, callback_matches_trusted_direct_writer_admission,
        callback_proves_terminal_execution, child_admission_record, child_completion_state,
        classify_child_runtime_registration, commander_continuation_binding,
        commander_convergence_proof_from_runtime, compile_commander_task_packet_components,
        complete_and_ack_callback_continuation, create_child_session,
        durable_child_admission_disposition_from_store, failed_session_retry_root,
        failed_session_runtime_fallback, intake_terminal_receipt, is_historical_terminal_runtime,
        lifecycle_store, payload_to_run_agent_request, populate_scope_evidence,
        pre_provider_commander_active_writer_evidence,
        pre_provider_commander_binding_recovery_evidence,
        pre_provider_commander_ledger_chain_evidence, pre_provider_zero_effect_failure_evidence,
        publish_admitted_pre_execution_failure_callback_from_store,
        publish_terminal_failure_callback_from_store, read_session_snapshot,
        ready_set_tracks_runtime_lease, register_and_activate_runtime,
        register_child_session_response, replay_terminal_callbacks_from_store,
        require_successful_runtime_dispatch, runtime_lease_from_snapshot,
        runtime_terminal_state_from_snapshot, task_ready_set_entry,
        terminal_commander_convergence_recovery_evidence, terminal_runtime_is_current,
        trusted_direct_thread_message, validate_child_runtime_identity,
        validate_delegated_input_digest, validate_existing_child_admission,
        validate_terminalization_identity, validated_child_execution_payload,
    };
    use crate::{build_state, services::manager::ServiceManager};
    use chrono::Utc;
    use lifecycle::{
        PlanStatus, ProviderConfig, RuntimeAggregate, RuntimeError, RuntimeProviderConfig,
        RuntimeState, SessionProjection, SessionState, StartCondition, TaskLeaseReadinessEvidence,
        TaskPlan, TaskReadySetEvidence, TaskSchedulingContractV1, TaskStep, ToolChoice,
    };
    use router_contract::{
        AcknowledgeChildCallbackEffectIdentity, AcknowledgeChildCallbackOutcome,
        AcknowledgeChildCallbackRequest, AcknowledgeChildCallbackResponse,
        COMMANDER_TASK_PACKET_SCHEMA_VERSION, CommanderMutationCounts,
        CommanderTaskPacketCompileResult, CommanderTaskPacketDispatchResponse,
        CommanderTaskPacketV1, RegisterChildSessionOutcome, RegisterChildSessionRequest,
    };
    use runtime_contract::{
        CommanderConvergenceProof, NATIVE_CODEX_EXECUTION_BINDING_SCHEMA_VERSION,
        NATIVE_CODEX_TASK_DELTA_SCHEMA_VERSION, NativeCodexExecutionBinding, NativeCodexTaskDelta,
        RunAgentRequest, TASK_CONTEXT_CAPSULE_SCHEMA_VERSION,
    };
    use serde_json::json;
    use session_lifecycle::{
        CallbackDeliveryRoute as LifecycleCallbackDeliveryRoute, CallbackEffectIdentity,
        ChildAdmissionOutcome, ChildAdmissionRecord, ChildReadySetIdentity,
        ContinuationDispatchRecord, DurableCallbackRecord, LifecycleCallbackProjection,
        LifecycleConfig, LifecycleRecordStage, SessionLifecycleStore, TerminalReceipt,
        TerminalReceiptIdentity, TerminalState, commander_store_path,
    };
    use session_log_contract::{
        RuntimeLeaseSnapshot, RuntimeLifecycleIdentity, SessionFeedEntry, SessionFeedEvent,
        SessionMetadata,
    };
    use std::collections::{BTreeMap, BTreeSet};
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    fn ready_set_contract() -> TaskSchedulingContractV1 {
        let mut contract = TaskSchedulingContractV1 {
            schema_version: lifecycle::TASK_SCHEDULING_CONTRACT_SCHEMA_VERSION.to_string(),
            mission_id: "mission-ready".to_string(),
            semantic_dispatch_key: "0".repeat(64),
            authority_mission_revision_sha256: "a".repeat(64),
            delegated_input_sha256: "b".repeat(64),
            task_context_capsule_semantic_sha256: "c".repeat(64),
            dependency_task_ids: vec![],
            exact_input_sha256s: vec!["d".repeat(64)],
            jspace_authorization_semantic_sha256: "e".repeat(64),
            read_scopes: vec!["src/**".to_string()],
            write_scopes: vec![],
            declared_targets: vec![],
            conflict_identities: vec![],
            maximum_parallel_runtime_workers: 24,
        };
        contract.semantic_dispatch_key = session_lifecycle::canonical_value_sha256(
            &contract.semantic_dispatch_identity("task-ready"),
        );
        contract
    }

    fn ready_set_evidence() -> TaskReadySetEvidence {
        TaskReadySetEvidence {
            evaluated_at_ms: Utc::now().timestamp_millis(),
            parent_session_can_transition_to_running: true,
            authority_mission_revision_sha256: "a".repeat(64),
            execution_terminal_task_ids: BTreeSet::new(),
            available_exact_input_sha256s: None,
            semantic_dispatch_identity_valid: true,
            candidate_scope_identity_valid: true,
            parallel_runtime_limit_supported: true,
            task_leases: vec![],
            lease_unknown_task_ids: BTreeSet::new(),
            scope_evaluated_task_ids: BTreeSet::new(),
            scope_conflicting_task_ids: BTreeSet::new(),
            scope_unknown_task_ids: BTreeSet::new(),
            active_runtime_workers: 0,
            requested_lease_id: None,
            claimed_task_id: None,
        }
    }

    #[test]
    fn ready_set_read_is_typed_unknown_without_exact_inputs_and_ready_with_exact_set() {
        let plan = TaskPlan {
            plan_summary: "plan".to_string(),
            detailed_tasks: vec![TaskStep {
                task_id: "task-ready".to_string(),
                start_condition: StartCondition::SessionIdle,
                scheduling_contract: Some(ready_set_contract()),
                ..TaskStep::default()
            }],
        };
        let mut evidence = ready_set_evidence();
        let entry = task_ready_set_entry(
            &plan,
            "task-ready",
            &"f".repeat(64),
            true,
            &evidence,
            &BTreeMap::new(),
        );
        assert_eq!(
            entry.state,
            router_contract::TaskReadySetWireState::TypedUnknown
        );
        assert_eq!(
            entry.reason_codes,
            vec!["TASK_READY_SET_EXACT_INPUT_AVAILABILITY_UNKNOWN"]
        );

        evidence.available_exact_input_sha256s = Some(BTreeSet::from(["d".repeat(64)]));
        let entry = task_ready_set_entry(
            &plan,
            "task-ready",
            &"f".repeat(64),
            true,
            &evidence,
            &BTreeMap::new(),
        );
        assert_eq!(entry.state, router_contract::TaskReadySetWireState::Ready);
    }

    #[test]
    fn global_active_admission_scope_conflict_is_not_limited_to_parent_plan() {
        let candidate = ready_set_contract();
        let mut evidence = ready_set_evidence();
        evidence.available_exact_input_sha256s = Some(BTreeSet::from(["d".repeat(64)]));
        evidence.task_leases.push(TaskLeaseReadinessEvidence {
            task_id: "other-commander:task-active".to_string(),
            lease_id: "lease-active".to_string(),
            lease_active: true,
            terminal: false,
        });
        let active = ChildReadySetIdentity::new(
            "mission-active",
            "task-active",
            "1".repeat(64),
            "2".repeat(64),
            "3".repeat(64),
            "4".repeat(64),
            "5".repeat(64),
            vec![],
            vec!["src/**".to_string()],
            vec![],
        );
        let identities = BTreeMap::from([("other-commander:task-active".to_string(), active)]);
        populate_scope_evidence(&candidate, &mut evidence, &identities);
        assert!(
            evidence
                .scope_conflicting_task_ids
                .contains("other-commander:task-active")
        );
    }

    #[test]
    fn multi_domain_union_is_transitive_to_conflict_claim_and_durable_identity() {
        let mut candidate = ready_set_contract();
        candidate.read_scopes = vec![
            "/tmp/effect-metadata/worktrees/task/**".to_string(),
            "/tmp/effect-worktree/src/**".to_string(),
        ];
        candidate.write_scopes = vec!["/tmp/effect-worktree/**".to_string()];
        candidate.declared_targets = vec!["/tmp/effect-worktree".to_string()];
        let base_claim = candidate.scope_claim_sha256();

        let active = ChildReadySetIdentity::new(
            "mission-active",
            "task-active",
            "1".repeat(64),
            "2".repeat(64),
            "3".repeat(64),
            "4".repeat(64),
            "5".repeat(64),
            vec![],
            vec!["/tmp/effect-metadata/**".to_string()],
            vec![],
        );
        assert_eq!(active.write_scopes, vec!["/tmp/effect-metadata/**"]);
        let mut evidence = ready_set_evidence();
        evidence.task_leases.push(TaskLeaseReadinessEvidence {
            task_id: "other-commander:task-active".to_string(),
            lease_id: "lease-active".to_string(),
            lease_active: true,
            terminal: false,
        });
        populate_scope_evidence(
            &candidate,
            &mut evidence,
            &BTreeMap::from([("other-commander:task-active".to_string(), active)]),
        );
        assert!(
            evidence
                .scope_conflicting_task_ids
                .contains("other-commander:task-active")
        );

        let worktree_active = ChildReadySetIdentity::new(
            "mission-worktree-active",
            "task-worktree-active",
            "b".repeat(64),
            "c".repeat(64),
            "d".repeat(64),
            "e".repeat(64),
            "f".repeat(64),
            vec![],
            vec!["/tmp/effect-worktree/**".to_string()],
            vec![],
        );
        let mut worktree_evidence = ready_set_evidence();
        worktree_evidence
            .task_leases
            .push(TaskLeaseReadinessEvidence {
                task_id: "other-commander:task-worktree-active".to_string(),
                lease_id: "lease-worktree-active".to_string(),
                lease_active: true,
                terminal: false,
            });
        populate_scope_evidence(
            &candidate,
            &mut worktree_evidence,
            &BTreeMap::from([(
                "other-commander:task-worktree-active".to_string(),
                worktree_active,
            )]),
        );
        assert!(
            worktree_evidence
                .scope_conflicting_task_ids
                .contains("other-commander:task-worktree-active")
        );

        let legacy = ChildReadySetIdentity::new(
            "mission-legacy",
            "task-legacy",
            "6".repeat(64),
            "7".repeat(64),
            "8".repeat(64),
            "9".repeat(64),
            "a".repeat(64),
            vec![],
            vec!["src/**".to_string()],
            vec![],
        );
        let mut legacy_evidence = ready_set_evidence();
        legacy_evidence
            .task_leases
            .push(TaskLeaseReadinessEvidence {
                task_id: "legacy:task".to_string(),
                lease_id: "legacy-lease".to_string(),
                lease_active: true,
                terminal: false,
            });
        populate_scope_evidence(
            &candidate,
            &mut legacy_evidence,
            &BTreeMap::from([("legacy:task".to_string(), legacy)]),
        );
        assert!(
            legacy_evidence
                .scope_unknown_task_ids
                .contains("legacy:task")
        );

        let mut changed = candidate.clone();
        changed.read_scopes = vec!["/tmp/effect-worktree/src/**".to_string()];
        assert_ne!(base_claim, changed.scope_claim_sha256());
        changed = candidate.clone();
        changed.write_scopes = vec!["/tmp/other-root/**".to_string()];
        assert_ne!(base_claim, changed.scope_claim_sha256());
        changed = candidate.clone();
        changed.declared_targets = vec!["/tmp/effect-worktree/other".to_string()];
        assert_ne!(base_claim, changed.scope_claim_sha256());
        changed = candidate;
        changed.jspace_authorization_semantic_sha256 = "f".repeat(64);
        assert_ne!(base_claim, changed.scope_claim_sha256());
    }

    #[test]
    fn terminalizing_runtime_remains_ready_set_owner_until_durable_terminal() {
        let lease = RuntimeLease {
            runtime_id: "runtime-terminalizing-ready-set".to_string(),
            lease_id: "lease-terminalizing-ready-set".to_string(),
            commander_session_id: "commander-terminalizing-ready-set".to_string(),
            transaction_id: "transaction-terminalizing-ready-set".to_string(),
            parent_mission_revision_sha256: Some("a".repeat(64)),
            delegated_input_sha256: Some("b".repeat(64)),
            task_id: Some("task-active".to_string()),
            goal_id: None,
            operator_override: false,
            receipt_event_seq: 0,
            slot_acquired: true,
            terminalizing: true,
        };

        assert!(ready_set_tracks_runtime_lease(
            "commander-terminalizing-ready-set",
            "child-terminalizing-ready-set",
            &lease,
        ));
        assert!(!ready_set_tracks_runtime_lease(
            "commander-terminalizing-ready-set",
            "commander-terminalizing-ready-set",
            &lease,
        ));
    }

    #[test]
    fn failed_session_registration_reuses_exact_latest_runtime_lineage() {
        let mut projection = SessionProjection {
            session_id: "session-retry".to_string(),
            state: SessionState::Failed,
            parent_id: None,
            task_plan: TaskPlan::default(),
            pending_user_inputs: Vec::new(),
            cancelled: false,
            runtime_ids: vec!["runtime-old".to_string(), "runtime-failed".to_string()],
            active_runtime_id: None,
        };

        assert_eq!(
            failed_session_runtime_fallback(&projection).expect("failed session fallback"),
            Some("runtime-failed".to_string())
        );

        projection.state = SessionState::Completed;
        assert_eq!(
            failed_session_runtime_fallback(&projection).expect("completed session starts fresh"),
            None
        );

        projection.state = SessionState::Failed;
        projection.runtime_ids.clear();
        assert!(
            failed_session_runtime_fallback(&projection)
                .expect_err("failed session without lineage must fail closed")
                .to_string()
                .contains("FAILED_SESSION_MISSING_RUNTIME_LINEAGE")
        );
    }

    #[test]
    fn failed_session_retry_recovers_root_before_legacy_unlinked_attempts() {
        let projection = SessionProjection {
            session_id: "session-retry".to_string(),
            state: SessionState::Failed,
            parent_id: None,
            task_plan: TaskPlan::default(),
            pending_user_inputs: Vec::new(),
            cancelled: false,
            runtime_ids: vec![
                "runtime-completed".to_string(),
                "runtime-root".to_string(),
                "runtime-legacy-retry-1".to_string(),
                "runtime-legacy-retry-2".to_string(),
            ],
            active_runtime_id: None,
        };
        let identities = std::collections::HashMap::from([
            (
                "runtime-completed",
                RetryRuntimeIdentity {
                    runtime_id: "runtime-completed".to_string(),
                    session_id: "session-retry".to_string(),
                    state: RuntimeState::Finished,
                    latest_user_input: Some("older completed task".to_string()),
                },
            ),
            (
                "runtime-root",
                RetryRuntimeIdentity {
                    runtime_id: "runtime-root".to_string(),
                    session_id: "session-retry".to_string(),
                    state: RuntimeState::Failed,
                    latest_user_input: Some("exact root task".to_string()),
                },
            ),
            (
                "runtime-legacy-retry-1",
                RetryRuntimeIdentity {
                    runtime_id: "runtime-legacy-retry-1".to_string(),
                    session_id: "session-retry".to_string(),
                    state: RuntimeState::Failed,
                    latest_user_input: Some("legacy wrapper prompt".to_string()),
                },
            ),
            (
                "runtime-legacy-retry-2",
                RetryRuntimeIdentity {
                    runtime_id: "runtime-legacy-retry-2".to_string(),
                    session_id: "session-retry".to_string(),
                    state: RuntimeState::Failed,
                    latest_user_input: Some("legacy wrapper prompt".to_string()),
                },
            ),
        ]);

        let root = failed_session_retry_root(
            &projection,
            "runtime-legacy-retry-2",
            "exact root task",
            |runtime_id| Ok(identities.get(runtime_id).cloned()),
        )
        .expect("legacy unlinked retries should reconcile to their failed root");
        assert_eq!(root, "runtime-root");

        let error = failed_session_retry_root(
            &projection,
            "runtime-legacy-retry-2",
            "legacy wrapper prompt",
            |runtime_id| Ok(identities.get(runtime_id).cloned()),
        )
        .expect_err("a rewritten retry prompt must not replace canonical root input");
        assert!(
            error
                .to_string()
                .contains("FAILED_SESSION_RETRY_ROOT_INPUT_MISMATCH")
        );
    }
    #[test]
    fn recovery_router_payload_rejects_caller_supplied_quiescence() {
        let request = json!({
            "receipt_id": "receipt-1",
            "database_path": "/tmp/session_log.sqlite3",
            "runtime_id": "runtime-1",
            "session_id": "session-1",
            "lease_id": "lease-1",
            "expected_lease_active": true,
            "expected_revision": 0,
            "expected_last_event_seq": 0,
            "expected_session_event_seq": 1,
            "expected_session_state": "interrupted",
            "reason": "unborn_runtime",
            "quiescence": {
                "active_turn": false
            }
        });

        let error = serde_json::from_value::<RouterRecoveryCloseRuntimeRequest>(request)
            .expect_err("caller-supplied quiescence proof must be rejected");
        assert!(error.to_string().contains("unknown field `quiescence`"));
    }

    #[tokio::test]
    async fn queued_recovery_writer_does_not_deadlock_nested_command_run() {
        let workspace = tempfile::tempdir().expect("nested command workspace");
        let state = build_state();
        let service = ExecutionService::new();
        service.set_session_lease_for_test("nested-session", true);
        let outer_turn_lease = service.admission.read().await;
        let (queued_tx, queued_rx) = tokio::sync::oneshot::channel();
        let writer_gate = Arc::clone(&service.admission);
        let writer = tokio::spawn(async move {
            queued_tx.send(()).expect("signal queued recovery writer");
            let _recovery = writer_gate.write().await;
        });
        queued_rx.await.expect("recovery writer queued");
        tokio::task::yield_now().await;
        assert!(!writer.is_finished());

        let nested = tokio::time::timeout(
            std::time::Duration::from_secs(1),
            service.command_run_request(
                &state,
                json!({
                    "session_id": "nested-session",
                    "runtime_id": "runtime-nested-session",
                    "session_directory": workspace.path().display().to_string(),
                    "arguments": {
                        "commands": [{
                            "command": "task_status",
                            "command_line": json!({
                                "status": "done",
                                "task_group": "nested admission canary"
                            }).to_string()
                        }]
                    },
                    "allowed_commands": ["task_status"]
                }),
                "nested-command-run-canary",
            ),
        )
        .await
        .expect("nested command_run must not wait behind recovery writer")
        .expect("nested command_run should finish");
        assert_eq!(nested["result"]["results"][0]["success"], true);

        drop(outer_turn_lease);
        tokio::time::timeout(std::time::Duration::from_secs(1), writer)
            .await
            .expect("recovery writer should acquire after outer turn release")
            .expect("recovery writer task should join");
    }

    #[tokio::test]
    async fn active_router_turn_denies_recovery_before_session_db_mutation() {
        let state = build_state();
        let service = ExecutionService::new();
        service.set_session_lease_for_test("active-recovery-session", true);

        let response = service
            .recovery_close_runtime(
                &state,
                json!({
                    "receipt_id": "active-recovery-receipt",
                    "database_path": "/tmp/session_log.sqlite3",
                    "runtime_id": "runtime-active-recovery-session",
                    "session_id": "active-recovery-session",
                    "lease_id": "lease-active-recovery-session",
                    "expected_lease_active": true,
                    "expected_revision": 0,
                    "expected_last_event_seq": 0,
                    "expected_session_event_seq": 1,
                    "expected_session_state": "interrupted",
                    "reason": "unborn_runtime"
                }),
            )
            .await
            .expect("active recovery denial");

        assert_eq!(response["result"]["outcome"], "runtime_live");
        assert_eq!(response["result"]["proof"]["active_turn"], true);
        assert_eq!(response["result"]["proof"]["running_turn"], true);
        assert_eq!(
            response["result"]["proof"]["global_active_session_count"],
            1
        );
    }

    #[tokio::test]
    async fn terminalizing_phase_is_quiescent_but_keeps_session_reserved() {
        let state = build_state();
        let service = ExecutionService::new();
        service.set_session_lease_for_test("terminalizing-session", true);
        service
            .mark_terminalizing("terminalizing-session", "runtime-terminalizing-session")
            .expect("mark exact runtime terminalizing");

        let proof = service
            .terminalization_quiescence_proof(
                &state,
                "terminalizing-session",
                "runtime-terminalizing-session",
            )
            .await
            .expect("terminalizing runtime should be quiescent without effects");
        assert!(proof.is_quiescent());
        assert_eq!(proof.global_active_session_count, 1);
        assert!(
            service
                .sessions
                .lock()
                .contains_key("terminalizing-session")
        );

        let probe = service
            .probe_sessions(&state, json!({ "session_ids": ["terminalizing-session"] }))
            .await
            .expect("probe terminalizing session");
        assert_eq!(probe["sessions"][0]["status"], "terminalizing");
        assert_eq!(probe["sessions"][0]["active_turn"], false);
    }

    #[test]
    fn terminalization_identity_requires_exact_database_runtime_session_and_lease() {
        let lease = RuntimeLease {
            runtime_id: "runtime-exact".to_string(),
            lease_id: "lease-exact".to_string(),
            commander_session_id: "commander-exact".to_string(),
            transaction_id: "transaction-exact".to_string(),
            parent_mission_revision_sha256: None,
            delegated_input_sha256: None,
            task_id: None,
            goal_id: None,
            operator_override: false,
            receipt_event_seq: 0,
            slot_acquired: true,
            terminalizing: true,
        };
        let mut snapshot = RuntimeLeaseSnapshot {
            database_path: "/tmp/session_log.sqlite3".to_string(),
            runtime_id: "runtime-exact".to_string(),
            session_id: "session-exact".to_string(),
            lifecycle: None,
            lease_id: Some("lease-exact".to_string()),
            lease_active: true,
            revision: 0,
            last_event_seq: 0,
            terminal: false,
            session_event_seq: 2,
            session_state: SessionState::Running,
            runtime_state: None,
        };
        validate_terminalization_identity(&snapshot, &lease, "session-exact", "runtime-exact")
            .expect("exact durable identity should pass");

        snapshot.lease_id = Some("lease-drift".to_string());
        assert!(
            validate_terminalization_identity(&snapshot, &lease, "session-exact", "runtime-exact",)
                .expect_err("lease drift must fail closed")
                .to_string()
                .contains("RUNTIME_TERMINALIZATION_DURABLE_IDENTITY_MISMATCH")
        );
    }

    #[test]
    fn child_runtime_replay_requires_exact_durable_lifecycle_identity() {
        let request = router_contract::RegisterChildSessionRequest {
            parent_session_id: "commander-exact".to_string(),
            parent_mission_revision_sha256: "a".repeat(64),
            commander_thread_id: Some("commander-thread-exact".to_string()),
            child_session_id: "child-exact".to_string(),
            child_runtime_id: "runtime-exact".to_string(),
            child_transaction_id: "transaction-exact".to_string(),
            child_lease_id: "lease-exact".to_string(),
            callback_request_id: "transaction-exact".to_string(),
            effect_id: "runtime-exact.message".to_string(),
            callback_delivery_route:
                router_contract::CallbackDeliveryRoute::TrustedTuraDirectThreadWriter,
            delegated_input_sha256: "b".repeat(64),
            session_directory: "/tmp/child-exact".to_string(),
            session_name: "child exact".to_string(),
            created_at_ms: 1_786_845_600_000,
            execution_payload: json!({"prompt": "delegated prompt"}),
        };
        let lifecycle = RuntimeLifecycleIdentity {
            commander_session_id: request.parent_session_id.clone(),
            transaction_id: request.child_transaction_id.clone(),
            parent_mission_revision_sha256: Some(request.parent_mission_revision_sha256.clone()),
            delegated_input_sha256: Some(request.delegated_input_sha256.clone()),
            task_id: Some("task-exact".to_string()),
            goal_id: None,
            operator_override: false,
            dispatch_runtime_id: request.child_runtime_id.clone(),
            dispatch_lease_id: request.child_lease_id.clone(),
            receipt_event_seq: 0,
        };
        let mut snapshot = RuntimeLeaseSnapshot {
            database_path: "/tmp/session.sqlite3".to_string(),
            runtime_id: request.child_runtime_id.clone(),
            session_id: request.child_session_id.clone(),
            lifecycle: Some(lifecycle),
            lease_id: Some(request.child_lease_id.clone()),
            lease_active: false,
            revision: 1,
            last_event_seq: 1,
            terminal: true,
            session_event_seq: 1,
            session_state: SessionState::Running,
            runtime_state: Some(RuntimeState::Cancelled),
        };
        validate_child_runtime_identity(&snapshot, &request, "task-exact")
            .expect("exact runtime replay identity");
        assert_eq!(
            classify_child_runtime_registration(&snapshot, &request)
                .expect("exact terminal runtime"),
            ChildRuntimeRegistrationState::Terminal
        );

        snapshot.lease_id = Some("changed-lease".to_string());
        assert!(
            classify_child_runtime_registration(&snapshot, &request)
                .expect_err("changed terminal lease must remain unsettled")
                .to_string()
                .contains("CHILD_ADMISSION_RUNTIME_STATE_UNSETTLED")
        );

        snapshot.lease_id = None;
        snapshot.lease_active = false;
        snapshot.revision = 0;
        snapshot.last_event_seq = 0;
        snapshot.terminal = false;
        snapshot.runtime_state = None;
        assert_eq!(
            classify_child_runtime_registration(&snapshot, &request)
                .expect("registered-before-activation runtime is resumable"),
            ChildRuntimeRegistrationState::Unborn
        );

        snapshot.lease_id = Some(request.child_lease_id.clone());
        snapshot.lease_active = true;
        assert_eq!(
            classify_child_runtime_registration(&snapshot, &request)
                .expect("exact active runtime must not be redispatched"),
            ChildRuntimeRegistrationState::Active
        );

        snapshot.lifecycle.as_mut().expect("lifecycle").task_id = Some("task-other".to_string());
        assert!(
            validate_child_runtime_identity(&snapshot, &request, "task-exact")
                .expect_err("runtime task drift must conflict")
                .to_string()
                .contains("CHILD_ADMISSION_RUNTIME_IDENTITY_CONFLICT")
        );
    }

    #[test]
    fn durable_lifecycle_snapshot_restores_callback_identity_and_runtime_terminal_semantics() {
        let lifecycle = RuntimeLifecycleIdentity {
            commander_session_id: "commander-durable".to_string(),
            transaction_id: "transaction-durable".to_string(),
            parent_mission_revision_sha256: None,
            delegated_input_sha256: None,
            task_id: Some("task-durable".to_string()),
            goal_id: Some("goal-durable".to_string()),
            operator_override: true,
            dispatch_runtime_id: "runtime-durable".to_string(),
            dispatch_lease_id: "lease-durable".to_string(),
            receipt_event_seq: 2,
        };
        let snapshot = RuntimeLeaseSnapshot {
            database_path: "/tmp/session_log.sqlite3".to_string(),
            runtime_id: "runtime-durable".to_string(),
            session_id: "child-durable".to_string(),
            lifecycle: Some(lifecycle),
            lease_id: Some("lease-durable".to_string()),
            lease_active: false,
            revision: 7,
            last_event_seq: 7,
            terminal: true,
            session_event_seq: 3,
            session_state: SessionState::Running,
            runtime_state: Some(RuntimeState::Cancelled),
        };
        let projection = SessionProjection {
            session_id: "child-durable".to_string(),
            state: SessionState::Interrupted,
            parent_id: Some("commander-durable".to_string()),
            task_plan: TaskPlan::default(),
            pending_user_inputs: Vec::new(),
            cancelled: false,
            runtime_ids: vec!["runtime-durable".to_string()],
            active_runtime_id: None,
        };

        let lease = runtime_lease_from_snapshot(&snapshot)
            .expect("durable lifecycle identity should reconstruct the callback owner");
        assert_eq!(lease.transaction_id, "transaction-durable");
        assert_eq!(lease.commander_session_id, "commander-durable");
        assert_eq!(lease.receipt_event_seq, 2);
        assert_eq!(
            runtime_terminal_state_from_snapshot(&snapshot, &projection)
                .expect("runtime terminal state must survive a later session continuation"),
            TerminalState::Cancelled
        );

        let mut legacy_snapshot = snapshot.clone();
        legacy_snapshot.runtime_state = None;
        let error = runtime_terminal_state_from_snapshot(&legacy_snapshot, &projection)
            .expect_err("legacy snapshots still require matching session projections");
        assert!(
            error
                .to_string()
                .contains("RUNTIME_CALLBACK_SESSION_STATE_MISMATCH")
        );
    }

    #[test]
    fn historical_terminal_runtime_requires_no_active_runtime_ownership() {
        assert!(is_historical_terminal_runtime(None, "runtime-old"));
        assert!(is_historical_terminal_runtime(
            Some("runtime-current"),
            "runtime-old"
        ));
        assert!(!is_historical_terminal_runtime(
            Some("runtime-current"),
            "runtime-current"
        ));
    }

    #[test]
    fn snapshot_terminal_receipt_replays_or_reuses_runtime_writer_receipt() {
        let lifecycle = RuntimeLifecycleIdentity {
            commander_session_id: "commander-replay".to_string(),
            transaction_id: "transaction-replay".to_string(),
            parent_mission_revision_sha256: None,
            delegated_input_sha256: None,
            task_id: Some("task-replay".to_string()),
            goal_id: Some("goal-replay".to_string()),
            operator_override: true,
            dispatch_runtime_id: "runtime-replay".to_string(),
            dispatch_lease_id: "lease-replay".to_string(),
            receipt_event_seq: 0,
        };
        let snapshot = RuntimeLeaseSnapshot {
            database_path: "/tmp/session_log.sqlite3".to_string(),
            runtime_id: "runtime-replay".to_string(),
            session_id: "child-replay".to_string(),
            lifecycle: Some(lifecycle),
            lease_id: Some("lease-replay".to_string()),
            lease_active: false,
            revision: 8,
            last_event_seq: 8,
            terminal: true,
            session_event_seq: 4,
            session_state: SessionState::Failed,
            runtime_state: Some(RuntimeState::Failed),
        };
        let lease = runtime_lease_from_snapshot(&snapshot).expect("durable callback identity");
        let event_id = "runtime-replay:8:session-projection";
        let entry = SessionFeedEntry {
            session_id: "child-replay".to_string(),
            cursor: 12,
            runtime_id: Some("runtime-replay".to_string()),
            event_id: event_id.to_string(),
            event: SessionFeedEvent::SessionProjectionUpdated {
                projection: SessionProjection {
                    session_id: "child-replay".to_string(),
                    state: SessionState::Failed,
                    parent_id: Some("commander-replay".to_string()),
                    task_plan: TaskPlan::default(),
                    pending_user_inputs: Vec::new(),
                    cancelled: false,
                    runtime_ids: vec!["runtime-replay".to_string()],
                    active_runtime_id: None,
                },
                session_name: None,
                updated_at: 1_787_970_243_996,
            },
        };

        let replay = ExecutionService::snapshot_terminal_receipt(
            &lease,
            &snapshot,
            &entry,
            TerminalState::Failed,
        )
        .expect("snapshot receipt");
        let mut original = TerminalReceipt::new(
            TerminalReceiptIdentity::new(
                "transaction-replay",
                event_id,
                0,
                "commander-replay",
                "child-replay",
                "runtime-replay",
                "lease-replay",
            ),
            TerminalState::Failed,
            1_787_970_243_996,
        );
        original.task_id = Some("task-replay".to_string());
        original.goal_id = Some("goal-replay".to_string());
        original.operator_override = true;
        original
            .audit_metadata
            .insert("runtime_event_seq".to_string(), json!(8));
        original
            .audit_metadata
            .insert("runtime_expected_revision".to_string(), json!(7));
        original
            .audit_metadata
            .insert("runtime_state".to_string(), json!(RuntimeState::Failed));
        original
            .audit_metadata
            .insert("dispatch_runtime_id".to_string(), json!("runtime-replay"));
        original
            .audit_metadata
            .insert("dispatch_lease_id".to_string(), json!("lease-replay"));

        assert_eq!(replay, original);
        assert!(!replay.audit_metadata.contains_key("session_state"));
        let root = tempfile::tempdir().expect("lifecycle root");
        let store = SessionLifecycleStore::open(
            root.path(),
            "commander-replay",
            LifecycleConfig::default(),
        )
        .expect("lifecycle store");
        store
            .write_terminal_receipt(&original)
            .expect("original runtime receipt");
        store
            .write_terminal_receipt(&replay)
            .expect("snapshot replay must be already durable, not conflicting");

        let mut delayed_projection = entry.clone();
        match &mut delayed_projection.event {
            SessionFeedEvent::SessionProjectionUpdated { updated_at, .. } => {
                *updated_at += 140;
            }
            _ => panic!("fixture must remain a projection event"),
        }
        let reconstructed = ExecutionService::snapshot_terminal_receipt(
            &lease,
            &snapshot,
            &delayed_projection,
            TerminalState::Failed,
        )
        .expect("delayed projection receipt");
        assert_ne!(reconstructed, original);
        ExecutionService::ensure_snapshot_terminal_receipt(
            &store,
            &lease,
            &snapshot,
            &delayed_projection,
            TerminalState::Failed,
        )
        .expect("the durable runtime receipt must outrank a later projection timestamp");
        assert_eq!(
            store
                .terminal_receipt("transaction-replay", event_id)
                .expect("durable runtime receipt"),
            original
        );
        let delivery = intake_terminal_receipt(
            &store,
            &delayed_projection,
            "runtime-replay",
            "transaction-replay",
            &lease,
            TerminalState::Failed,
        )
        .expect("existing durable receipt must remain intake-compatible")
        .expect("terminal delivery identity");
        assert_eq!(delivery.runtime_id, "runtime-replay");
        assert_eq!(store.readback().expect("readback").applied_receipts, 1);
    }

    #[test]
    fn payload_to_run_agent_request_injects_authoritative_session_id() {
        let request = EnqueueTurnRequest {
            runtime_id: "runtime-1".to_string(),
            session_id: "session-authoritative".to_string(),
            payload: json!({
                "session_id": "stale-session",
                "prompt": "hello",
                "model": "openai/gpt-test",
                "worker_env": { "TURA_REASONING_EFFORT": "low" }
            }),
        };

        let run = payload_to_run_agent_request(
            &request,
            "lease-test",
            Some("runtime-failed".to_string()),
        )
        .expect("valid enqueue payload should become run-agent request");

        assert_eq!(run.session_id.as_deref(), Some("session-authoritative"));
        assert_eq!(run.runtime_id, "runtime-1");
        assert_eq!(run.fallback_from_id.as_deref(), Some("runtime-failed"));
        assert_eq!(run.prompt.as_deref(), Some("hello"));
        assert_eq!(run.model.as_deref(), Some("openai/gpt-test"));
        assert_eq!(
            run.worker_env
                .get("TURA_REASONING_EFFORT")
                .map(String::as_str),
            Some("low")
        );
    }

    #[test]
    fn payload_to_run_agent_request_reports_invalid_payload_shape() {
        let request = EnqueueTurnRequest {
            runtime_id: "runtime-invalid".to_string(),
            session_id: "session-invalid".to_string(),
            payload: json!({
                "worker_env": "not-an-object"
            }),
        };

        let error = payload_to_run_agent_request(&request, "lease-test", None)
            .expect_err("invalid worker_env shape should be rejected");

        assert!(
            error.to_string().contains("invalid run-agent payload")
                && error.to_string().contains("runtime-invalid")
                && error.to_string().contains("session-invalid"),
            "invalid payload error should include runtime and session context: {error}"
        );
    }

    #[test]
    fn terminal_runtime_chain_accepts_fallback_and_ignores_prior_turns() {
        let projection = SessionProjection {
            session_id: "child-1".to_string(),
            state: SessionState::Completed,
            parent_id: Some("commander-1".to_string()),
            task_plan: TaskPlan::default(),
            pending_user_inputs: Vec::new(),
            cancelled: false,
            runtime_ids: vec![
                "old-runtime".to_string(),
                "dispatch-runtime".to_string(),
                "fallback-runtime".to_string(),
            ],
            active_runtime_id: None,
        };

        assert!(!terminal_runtime_is_current(
            &projection,
            "child-1",
            "dispatch-runtime",
            "old-runtime",
        )
        .expect("historical runtime should be ignored"));
        assert!(
            terminal_runtime_is_current(
                &projection,
                "child-1",
                "dispatch-runtime",
                "fallback-runtime",
            )
            .expect("latest fallback should be accepted")
        );
        let error = terminal_runtime_is_current(
            &projection,
            "child-1",
            "dispatch-runtime",
            "dispatch-runtime",
        )
        .expect_err("non-latest dispatch runtime must not terminate a fallback chain");
        assert!(
            error
                .to_string()
                .contains("TERMINAL_FEED_RUNTIME_CHAIN_MISMATCH")
        );
    }

    #[test]
    fn fallback_receipt_intake_is_exactly_once_and_preserves_dispatch_identity() {
        let root = tempfile::tempdir().expect("lifecycle root");
        let store =
            SessionLifecycleStore::open(root.path(), "commander-1", LifecycleConfig::default())
                .expect("lifecycle store");
        let lease = RuntimeLease {
            runtime_id: "dispatch-runtime".to_string(),
            lease_id: "dispatch-lease".to_string(),
            commander_session_id: "commander-1".to_string(),
            transaction_id: "transaction-1".to_string(),
            parent_mission_revision_sha256: None,
            delegated_input_sha256: None,
            task_id: Some("task-1".to_string()),
            goal_id: Some("goal-1".to_string()),
            operator_override: true,
            receipt_event_seq: 0,
            slot_acquired: true,
            terminalizing: false,
        };
        let projection = SessionProjection {
            session_id: "child-1".to_string(),
            state: SessionState::Completed,
            parent_id: Some("commander-1".to_string()),
            task_plan: TaskPlan::default(),
            pending_user_inputs: Vec::new(),
            cancelled: false,
            runtime_ids: vec![
                "dispatch-runtime".to_string(),
                "fallback-runtime".to_string(),
            ],
            active_runtime_id: None,
        };
        let entry = SessionFeedEntry {
            session_id: "child-1".to_string(),
            cursor: 9,
            runtime_id: Some("fallback-runtime".to_string()),
            event_id: "fallback-runtime:5:session-projection".to_string(),
            event: SessionFeedEvent::SessionProjectionUpdated {
                projection: projection.clone(),
                session_name: None,
                updated_at: 10,
            },
        };
        let mut receipt = TerminalReceipt::new(
            TerminalReceiptIdentity::new(
                "transaction-1",
                entry.event_id.clone(),
                0,
                "commander-1",
                "child-1",
                "fallback-runtime",
                "fallback-lease",
            ),
            TerminalState::Completed,
            10,
        );
        receipt.task_id = Some("task-1".to_string());
        receipt.goal_id = Some("goal-1".to_string());
        receipt.operator_override = true;
        receipt
            .audit_metadata
            .insert("dispatch_runtime_id".to_string(), json!("dispatch-runtime"));
        receipt
            .audit_metadata
            .insert("dispatch_lease_id".to_string(), json!("dispatch-lease"));
        store
            .write_terminal_receipt(&receipt)
            .expect("durable fallback receipt");

        let first = intake_terminal_receipt(
            &store,
            &entry,
            "fallback-runtime",
            "transaction-1",
            &lease,
            TerminalState::Completed,
        )
        .expect("first intake")
        .expect("terminal delivery");
        let duplicate = intake_terminal_receipt(
            &store,
            &entry,
            "fallback-runtime",
            "transaction-1",
            &lease,
            TerminalState::Completed,
        )
        .expect("duplicate intake")
        .expect("duplicate terminal delivery");
        assert_eq!(first, duplicate);
        assert_eq!(first.runtime_id, "fallback-runtime");
        assert_eq!(store.readback().expect("readback").applied_receipts, 1);
        assert_eq!(store.readback().expect("readback").pending_receipts, 0);
    }

    #[test]
    fn terminal_session_command_projection_without_runtime_is_not_a_receipt() {
        let service = ExecutionService::new();
        let entry = SessionFeedEntry {
            session_id: "child-1".to_string(),
            cursor: 20,
            runtime_id: None,
            event_id: "session-command-1:session-projection".to_string(),
            event: SessionFeedEvent::SessionProjectionUpdated {
                projection: SessionProjection {
                    session_id: "child-1".to_string(),
                    state: SessionState::Completed,
                    parent_id: Some("commander-1".to_string()),
                    task_plan: TaskPlan::default(),
                    pending_user_inputs: Vec::new(),
                    cancelled: false,
                    runtime_ids: vec!["dispatch-runtime".to_string()],
                    active_runtime_id: None,
                },
                session_name: None,
                updated_at: 10,
            },
        };

        assert_eq!(
            service
                .intake_terminal_feed_entry(&entry, "transaction-1")
                .expect("session-level terminal projections are not receipt failures"),
            None
        );
    }

    #[tokio::test]
    async fn cancel_idle_turn_reports_idle_without_worker_stop() {
        let state = build_state();
        let response = ExecutionService::new()
            .cancel_turn(
                &state,
                json!({
                    "session_id": "idle-session",
                    "runtime_id": "runtime-idle-session"
                }),
            )
            .await;

        assert_eq!(response["status"], "idle");
        assert_eq!(response["session_id"], "idle-session");
        assert_eq!(response["stopped_worker"], false);
    }

    #[tokio::test]
    async fn cancel_active_turn_fails_closed_without_durable_runtime() {
        let state = build_state();
        let service = ExecutionService::new();
        service.set_session_lease_for_test("active-session", true);

        let response = service
            .cancel_turn(
                &state,
                json!({
                    "session_id": "active-session",
                    "runtime_id": "runtime-active-session"
                }),
            )
            .await;

        assert_eq!(response["status"], "error");
        assert_eq!(response["session_id"], "active-session");
        assert_eq!(response["stopped_worker"], false);
        assert_eq!(response["runtime_terminalized"], false);
        assert_eq!(response["terminalization_pending"], true);
        assert!(
            response["terminalization_error"]
                .as_str()
                .is_some_and(|error| !error.trim().is_empty())
        );
        assert!(
            service
                .sessions
                .lock()
                .get("active-session")
                .is_some_and(|lease| lease.terminalizing)
        );
    }

    #[tokio::test]
    async fn cancel_active_turn_drains_router_owned_command_run() {
        let state = build_state();
        let service = ExecutionService::new();
        service.set_session_lease_for_test("command-session", true);
        let workspace = tempfile::tempdir().expect("workspace");
        let command = if cfg!(windows) {
            "Test-Path .; Start-Sleep -Seconds 5".to_string()
        } else {
            "find . -maxdepth 0; sleep 5".to_string()
        };
        let request = json!({
            "session_id": "command-session",
            "runtime_id": "runtime-command-session",
            "session_directory": workspace.path().display().to_string(),
            "arguments": {
                "commands": [{
                    "command": "shell_command",
                    "command_line": json!({
                        "command": command,
                        "timeout_ms": 30_000
                    }).to_string()
                }]
            }
        });
        let running = {
            let command_run = state.command_run.clone();
            tokio::spawn(async move {
                command_run
                    .execute_with_request_id(request, Some("command-session-execution"))
                    .await
            })
        };
        let started = Instant::now();
        while state
            .command_run
            .active_count_for_session("command-session")
            == 0
            && started.elapsed() < Duration::from_secs(2)
        {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }

        let response = service
            .cancel_turn(
                &state,
                json!({
                    "session_id": "command-session",
                    "runtime_id": "runtime-command-session"
                }),
            )
            .await;

        assert_eq!(response["status"], "error");
        assert_eq!(response["active_command_runs_cancelled"], 1);
        assert_eq!(response["active_command_runs_remaining"], 0);
        assert_eq!(response["runtime_terminalized"], false);
        assert_eq!(response["terminalization_pending"], true);
        tokio::time::timeout(Duration::from_secs(2), running)
            .await
            .expect("cancelled command task should terminate promptly")
            .expect("cancelled command task should join")
            .expect("cancelled command response should remain deterministic");
        assert!(
            service
                .sessions
                .lock()
                .get("command-session")
                .is_some_and(|lease| lease.terminalizing)
        );
        assert!(
            !service
                .retained_slots
                .lock()
                .contains_key("command-session")
        );
    }

    #[tokio::test]
    async fn cancelled_runtime_cannot_publish_a_retained_slot() {
        let service = ExecutionService::new();
        service.set_session_lease_for_test("cancelled-session", true);
        let permit = service.runtime_slots.acquire(1).await;
        service.sessions.lock().remove("cancelled-session");

        let permit = service
            .retain_runtime_slot_if_current(
                "cancelled-session",
                "runtime-cancelled-session",
                permit,
            )
            .expect_err("removed lease must reject retained-slot publication");
        drop(permit);

        assert!(
            !service
                .retained_slots
                .lock()
                .contains_key("cancelled-session")
        );
    }

    #[tokio::test]
    async fn stale_runtime_cancel_does_not_remove_the_current_lease() {
        let state = build_state();
        let service = ExecutionService::new();
        service.set_session_lease_for_test("active-session", true);

        let response = service
            .cancel_turn(
                &state,
                json!({
                    "session_id": "active-session",
                    "runtime_id": "runtime-stale"
                }),
            )
            .await;

        assert_eq!(response["status"], "idle");
        assert!(service.sessions.lock().contains_key("active-session"));
    }

    #[test]
    fn execution_service_starts_with_no_active_runtime_workers() {
        let manager = ServiceManager::new();

        assert_eq!(manager.count_workers_with_prefix("runtime_worker:"), 0);
    }

    #[test]
    fn callback_execution_terminal_evidence_excludes_unsettled_effects() {
        let callback = |effect_kind: &str| LifecycleCallbackProjection {
            stage: LifecycleRecordStage::Intaken,
            transaction_id: "transaction-a".to_string(),
            event_id: "event-a".to_string(),
            child_session_id: "child-a".to_string(),
            runtime_id: "runtime-a".to_string(),
            lease_id: "lease-a".to_string(),
            terminal_receipt_sha256: "a".repeat(64),
            terminal_state: TerminalState::Completed,
            callback_payload_sha256: "b".repeat(64),
            transport_payload_sha256: "c".repeat(64),
            parent_mission_revision_sha256: "d".repeat(64),
            commander_thread_id: Some("thread-a".to_string()),
            callback_delivery_route: Some(
                LifecycleCallbackDeliveryRoute::TrustedTuraDirectThreadWriter,
            ),
            delegated_input_sha256: "e".repeat(64),
            effect_kind: effect_kind.to_string(),
            effect_identity_sha256: "f".repeat(64),
        };

        assert!(callback_proves_terminal_execution(&callback("exact")));
        assert!(callback_proves_terminal_execution(&callback(
            "proven_zero_effect"
        )));
        assert!(!callback_proves_terminal_execution(&callback(
            "unsettled_effect"
        )));
    }

    #[tokio::test]
    async fn probe_sessions_reports_active_and_inactive_sessions() {
        let state = build_state();
        let service = ExecutionService::new();
        service.set_session_lease_for_test("active-session", true);

        let response = service
            .probe_sessions(
                &state,
                json!({ "session_ids": ["active-session", "inactive-session"] }),
            )
            .await
            .expect("probe sessions");

        let sessions = response["sessions"]
            .as_array()
            .expect("sessions array should be present");
        assert_eq!(sessions[0]["session_id"], "active-session");
        assert_eq!(sessions[0]["status"], "running");
        assert_eq!(sessions[0]["active_turn"], true);
        assert_eq!(sessions[0]["running_turn"], true);
        assert_eq!(sessions[1]["session_id"], "inactive-session");
        assert_eq!(sessions[1]["status"], "inactive");
    }

    #[tokio::test]
    async fn probe_sessions_reports_queued_turns_as_active_without_worker() {
        let state = build_state();
        let service = ExecutionService::new();
        service.set_session_lease_for_test("queued-session", false);

        let response = service
            .probe_sessions(&state, json!({ "session_ids": ["queued-session"] }))
            .await
            .expect("probe sessions");

        let sessions = response["sessions"]
            .as_array()
            .expect("sessions array should be present");
        assert_eq!(sessions[0]["session_id"], "queued-session");
        assert_eq!(sessions[0]["runtime_id"], "runtime-queued-session");
        assert_eq!(sessions[0]["status"], "queued");
        assert_eq!(sessions[0]["active_turn"], true);
        assert_eq!(sessions[0]["queued_turn"], true);
        assert_eq!(sessions[0]["worker_alive"], false);
    }

    #[tokio::test]
    async fn execution_status_exposes_retained_and_command_liveness_evidence() {
        let state = build_state();
        let service = ExecutionService::new();
        service.set_session_lease_for_test("status-session", true);

        let response = service.status(&state).await;

        assert_eq!(response["status"], "ok");
        assert_eq!(response["active_session_count"], 1);
        assert_eq!(response["active_command_runs"], 0);
        assert_eq!(response["sessions"][0]["session_id"], "status-session");
        assert_eq!(response["sessions"][0]["slot_acquired"], true);
        assert_eq!(response["sessions"][0]["terminalizing"], false);
        assert_eq!(response["sessions"][0]["retained_slot"], false);
    }

    #[tokio::test]
    async fn enqueue_turn_reports_active_session_as_structured_payload_without_dispatch() {
        let state = build_state();
        let service = ExecutionService::new();
        service.set_session_lease_for_test("active-session", true);

        let response = service
            .enqueue_turn_request(
                &state,
                json!({
                    "runtime_id": "active-runtime-2",
                    "session_id": "active-session",
                    "payload": {
                        "prompt": "append instead of failing"
                    }
                }),
                "active-session-test",
            )
            .await
            .expect("active-session rejection is a gateway-handled payload");

        assert_eq!(response["ok"], false);
        assert_eq!(response["code"], "session_active_turn");
        assert_eq!(response["session_id"], "active-session");
        assert_eq!(response["runtime_id"], "runtime-active-session");
        assert!(service.sessions.lock().contains_key("active-session"));
        assert_eq!(
            state.manager.count_workers_with_prefix("runtime_worker:"),
            0
        );
    }

    #[tokio::test]
    async fn public_enqueue_rejects_self_attested_native_codex_execution() {
        let state = build_state();
        let service = ExecutionService::new();
        let error = service
            .enqueue_turn_request(
                &state,
                json!({
                    "runtime_id": "runtime-direct-native-ipc",
                    "session_id": "session-direct-native-ipc",
                    "payload": {
                        "prompt": "self-attested Native request",
                        "native_codex_execution": {}
                    }
                }),
                "direct-native-ipc",
            )
            .await
            .expect_err("public enqueue cannot bypass Commander admission");

        assert_eq!(
            error.to_string(),
            "NATIVE_CODEX_COMMANDER_ADMISSION_REQUIRED"
        );
        assert!(service.sessions.lock().is_empty());
        assert_eq!(state.manager.count_workers_with_prefix(""), 0);
    }

    #[tokio::test]
    async fn acquire_runtime_slot_queues_above_runtime_worker_limit_instead_of_rejecting() {
        let service = Arc::new(ExecutionService::new());
        let mut permits = Vec::new();
        let configured_limit = 6;
        for index in 0..configured_limit {
            permits.push(
                service
                    .acquire_runtime_slot(&format!("running-{index}"), configured_limit)
                    .await
                    .expect("initial runtime slots should be available"),
            );
        }

        service.set_session_lease_for_test("queued-session", false);
        let queued_service = Arc::clone(&service);
        let queued = tokio::spawn(async move {
            queued_service
                .acquire_runtime_slot("queued-session", configured_limit)
                .await
                .expect("queued turn should acquire the released runtime slot")
        });

        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        assert!(
            !queued.is_finished(),
            "turn above the runtime worker limit should wait in the queue, not fail immediately"
        );

        drop(permits.pop());
        let permit = tokio::time::timeout(std::time::Duration::from_secs(1), queued)
            .await
            .expect("queued turn should resume after a runtime slot is released")
            .expect("queued task should not panic");
        drop(permit);
    }

    fn callback_receipt(terminal_state: TerminalState) -> TerminalReceipt {
        let mut receipt = TerminalReceipt::new(
            TerminalReceiptIdentity::new(
                "transaction-callback",
                "event-callback",
                0,
                "commander-callback",
                "child-callback",
                "runtime-callback",
                "lease-callback",
            ),
            terminal_state,
            1_786_845_600_000,
        );
        receipt.audit_metadata.insert(
            "parent_mission_revision_sha256".to_string(),
            json!("a".repeat(64)),
        );
        receipt.audit_metadata.insert(
            "delegated_input_sha256".to_string(),
            json!(session_lifecycle::canonical_value_sha256(&json!(
                "delegated prompt"
            ))),
        );
        receipt
    }

    fn bind_direct_writer_callback(mut callback: DurableCallbackRecord) -> DurableCallbackRecord {
        callback.commander_thread_id = Some("commander-thread-1".to_string());
        callback.callback_delivery_route =
            Some(session_lifecycle::CallbackDeliveryRoute::TrustedTuraDirectThreadWriter);
        callback
    }

    fn accept_test_direct_delivery(
        store: &SessionLifecycleStore,
        record: &ContinuationDispatchRecord,
    ) {
        let message =
            serde_json::to_string(&record.parent_input).expect("serialize direct delivery message");
        store
            .begin_callback_continuation_direct_delivery(record, &message)
            .expect("persist direct delivery attempt");
        store
            .mark_callback_continuation_delivery_accepted(
                record,
                &json!({
                    "schema_version": "test_direct_delivery_acceptance_v1",
                    "request_id": record.request_id,
                }),
            )
            .expect("persist accepted direct delivery");
    }

    fn reconcile_test_direct_delivery(
        store: &SessionLifecycleStore,
        record: &ContinuationDispatchRecord,
    ) {
        let message =
            serde_json::to_string(&record.parent_input).expect("serialize direct delivery message");
        store
            .begin_callback_continuation_direct_delivery(record, &message)
            .expect("persist direct delivery attempt");
        store
            .mark_callback_continuation_delivery_reconciled(
                record,
                &json!({
                    "schema_version": "test_direct_delivery_reconciliation_v1",
                    "request_id": record.request_id,
                }),
            )
            .expect("persist reconciled direct delivery");
    }

    fn bind_test_convergence_proof(
        store: &SessionLifecycleStore,
        record: &ContinuationDispatchRecord,
    ) -> ContinuationDispatchRecord {
        let effect = serde_json::to_value(&record.effect_identity).expect("effect value");
        let proof = CommanderConvergenceProof {
            schema_version: runtime_contract::COMMANDER_CONVERGENCE_PROOF_SCHEMA_VERSION
                .to_string(),
            request_id: record.request_id.clone(),
            callback_payload_sha256: record.callback_payload_sha256.clone(),
            effect_identity_sha256: session_lifecycle::canonical_value_sha256(&effect),
            child_session_id: record.child_session_id.clone(),
            child_transaction_id: record.child_transaction_id.clone(),
            child_runtime_id: record.child_runtime_id.clone(),
            requested_action: record.requested_action.clone(),
            target_thread_id: record
                .commander_thread_id
                .clone()
                .expect("direct writer target"),
            pre_revision_sha256: record.parent_mission_revision_sha256.clone(),
            post_revision_sha256: "b".repeat(64),
            target_turn_id: "turn-direct-writer-1".to_string(),
            final_assistant_sha256: "c".repeat(64),
        };
        store
            .bind_callback_continuation_convergence_proof(record, &proof)
            .expect("bind test convergence proof");
        let mut bound = record.clone();
        bound.convergence_proof = Some(proof);
        bound
    }

    fn direct_writer_callback_record() -> DurableCallbackRecord {
        let receipt = callback_receipt(TerminalState::Completed);
        let callback = DurableCallbackRecord::new(
            &receipt,
            json!("child result"),
            json!({"kind": "gateway.callback"}),
            "a".repeat(64),
            session_lifecycle::canonical_value_sha256(&json!("delegated prompt")),
            CallbackEffectIdentity::Exact {
                effect_id: "message-1".to_string(),
            },
        )
        .expect("callback");
        bind_direct_writer_callback(callback)
    }

    fn callback_continuation_record() -> ContinuationDispatchRecord {
        ContinuationDispatchRecord::from_callback(&direct_writer_callback_record())
            .expect("continuation")
    }

    #[test]
    fn accepted_direct_delivery_replay_reconciles_without_second_send_admission() {
        let root = tempfile::tempdir().expect("lifecycle root");
        let (store, _) = durable_callback_fixture(root.path(), TerminalState::Completed);
        admit_callback_child(&store, &"a".repeat(64));
        let callback = direct_writer_callback_record();
        store.publish_callback(&callback).expect("publish callback");
        store
            .mark_callback_intaken(
                &callback.transaction_id,
                &callback.event_id,
                &callback.callback_payload_sha256,
            )
            .expect("intake callback");
        let record =
            ContinuationDispatchRecord::from_callback(&callback).expect("continuation record");
        store
            .prepare_callback_continuation(&record)
            .expect("prepare continuation");
        let (message, _, _) = trusted_direct_thread_message(&record).expect("direct message");

        assert!(matches!(
            admit_trusted_direct_thread_delivery(&store, &record, &message)
                .expect("first delivery admission"),
            DirectDeliveryAdmission::Send
        ));
        store
            .mark_callback_continuation_delivery_accepted(&record, &json!({"accepted": true}))
            .expect("accept first delivery");

        let current = match admit_trusted_direct_thread_delivery(&store, &record, &message)
            .expect("replayed delivery admission")
        {
            DirectDeliveryAdmission::Reconcile(current) => current,
            DirectDeliveryAdmission::Send => panic!("replay must not admit a second send"),
            DirectDeliveryAdmission::Settled(status) => {
                panic!("accepted-only delivery is not settled: {status}")
            }
        };
        store
            .mark_callback_continuation_delivery_reconciled(&current, &json!({"reconciled": true}))
            .expect("reconcile first delivery");

        assert!(matches!(
            admit_trusted_direct_thread_delivery(&store, &record, &message)
                .expect("settled replay admission"),
            DirectDeliveryAdmission::Settled("direct_delivery_reconciled_awaiting_commander_ack")
        ));
    }

    fn callback_parent_metadata() -> SessionMetadata {
        SessionMetadata {
            session_directory: "/tmp/parent-official-codex".to_string(),
            model: Some("official_codex_app_server/gpt-5.6-sol".to_string()),
            agent: Some("balanced".to_string()),
            session_type: "coding".to_string(),
            kill_processes_on_start: false,
            validator_enabled: false,
            force_planning: false,
            model_variant: None,
            model_acceleration_enabled: false,
            disable_permission_restrictions: true,
            use_last_tool_call_response: false,
            auto_session_name: false,
            context_tokens: lifecycle::ContextTokenStats::default(),
            runtime_usage: json!({}),
        }
    }

    fn assert_callback_parent_identity(payload: &serde_json::Value) {
        assert_eq!(payload["directory"], "/tmp/parent-official-codex");
        assert_eq!(payload["model"], "official_codex_app_server/gpt-5.6-sol");
        assert_eq!(payload["agent"], "balanced");
        assert_eq!(payload["session_type"], "coding");
    }

    #[test]
    fn callback_continuation_first_dispatch_inherits_parent_provider_identity() {
        let record = callback_continuation_record();
        let payload = callback_continuation_payload(&record, &callback_parent_metadata(), None)
            .expect("first continuation payload");

        assert_callback_parent_identity(&payload);
        assert_eq!(payload["lifecycle"]["transaction_id"], record.request_id);
        assert_eq!(
            payload["lifecycle"]["commander_session_id"],
            record.commander_session_id
        );
        assert!(payload["lifecycle"]["commander_continuation"].is_null());
    }

    #[test]
    fn callback_continuation_fallback_inherits_parent_provider_identity() {
        let record = callback_continuation_record();
        let binding = commander_continuation_binding(&record).expect("continuation binding");
        let payload =
            callback_continuation_payload(&record, &callback_parent_metadata(), Some(&binding))
                .expect("fallback continuation payload");

        assert_callback_parent_identity(&payload);
        assert_eq!(
            payload["lifecycle"]["commander_continuation"],
            serde_json::to_value(binding).expect("serialized binding")
        );
        assert_eq!(payload["lifecycle"]["transaction_id"], record.request_id);
    }

    fn pre_provider_route_admission_failure() -> RuntimeAggregate {
        let now = Utc::now();
        let mut runtime = RuntimeAggregate::new(
            "callback-continuation-runtime-pre-provider".to_string(),
            "commander-callback".to_string(),
            "balanced".to_string(),
            RuntimeProviderConfig {
                base: ProviderConfig {
                    tura_llm_name: "codex".to_string(),
                    default_model_tier: None,
                    current_model: Some("gpt-5.6-luna".to_string()),
                    stream: true,
                    temperature: 0.0,
                    max_tokens: 256,
                    tool_choice: ToolChoice::Auto,
                    time_out_ms: 1_000,
                },
                thinking: false,
                provider_name: "codex".to_string(),
                model_name: "gpt-5.6-luna".to_string(),
                provider_url_name: "local".to_string(),
                llm_provider_name: "codex".to_string(),
            },
            now,
        );
        runtime.mark_called(now).expect("call started");
        runtime
            .mark_waiting_first_token()
            .expect("waiting first token");
        runtime
            .set_input(json!({"prompt": "continue callback"}))
            .expect("input captured");
        let message = "official Codex admission rejected route: config error: legacy provider 'codex' is disabled; use 'official_codex_app_server'";
        runtime
            .set_output(json!({"error": message}))
            .expect("local diagnostic captured");
        runtime
            .finish_failure(
                now,
                RuntimeError {
                    error_code: Some("PROVIDER_ROUTE_ADMISSION_REJECTED".to_string()),
                    error_text: Some(message.to_string()),
                    retry_allowed: false,
                    fallback_allowed: false,
                    fallback_to_id: None,
                },
                RuntimeState::Failed,
                None,
            )
            .expect("runtime failed");
        runtime
    }

    #[test]
    fn provider_route_admission_failure_proves_pre_provider_zero_effect() {
        let runtime = pre_provider_route_admission_failure();
        let evidence = pre_provider_zero_effect_failure_evidence(&runtime)
            .expect("exact local admission rejection is recoverable");
        assert_eq!(
            pre_provider_zero_effect_failure_evidence(&runtime),
            Some(evidence),
            "evidence identity must be deterministic"
        );
    }

    #[test]
    fn commander_active_writer_rejection_proves_pre_submit_zero_effect() {
        let mut runtime = pre_provider_route_admission_failure();
        runtime.provider.provider_name = "official_codex_app_server/gpt-5.6-sol".to_string();
        runtime.provider.llm_provider_name = "official_codex_app_server".to_string();
        let message = "official Codex App Server returned an error for thread/resume: {\"code\":-32600,\"message\":\"thread commander-thread-1 already has an active writer\"}";
        runtime.output = Some(json!({"error": message}));
        let error = runtime.error.as_mut().expect("runtime error");
        error.error_code = Some("OFFICIAL_CODEX_APP_SERVER_FAILED".to_string());
        error.error_text = Some(message.to_string());

        assert!(
            pre_provider_commander_active_writer_evidence(&runtime).is_some(),
            "exact server-side writer rejection is deferred before provider effects"
        );

        runtime.provider.llm_provider_name = "openai".to_string();
        assert!(
            pre_provider_commander_active_writer_evidence(&runtime).is_none(),
            "a non-official provider must not borrow the writer-busy recovery class"
        );
    }

    #[test]
    fn commander_binding_mismatch_is_pre_submit_zero_effect() {
        let mut runtime = pre_provider_route_admission_failure();
        runtime.provider.provider_name = "official_codex_app_server/gpt-5.6-sol".to_string();
        runtime.provider.llm_provider_name = "official_codex_app_server".to_string();
        let message = "COMMANDER_CONTINUATION_BINDING_INVALID: runtime/request identity mismatch";
        runtime.output = Some(json!({"error": message}));
        let error = runtime.error.as_mut().expect("runtime error");
        error.error_code = Some("OFFICIAL_CODEX_APP_SERVER_FAILED".to_string());
        error.error_text = Some(message.to_string());

        assert!(
            pre_provider_commander_binding_recovery_evidence(&runtime).is_some(),
            "local binding validation fails before provider submission"
        );
    }

    #[test]
    fn commander_preledger_chain_rejection_is_pre_submit_zero_effect() {
        let mut runtime = pre_provider_route_admission_failure();
        runtime.provider.provider_name = "official_codex_app_server/gpt-5.6-sol".to_string();
        runtime.provider.llm_provider_name = "official_codex_app_server".to_string();
        let message = "OFFICIAL_CODEX_INTERRUPTED_RECOVERY_UNCERTAIN_EFFECT: effect 0 is not durably reconciled: runtime execution ledger: execution ledger fallback source is not durable";
        runtime.output = Some(json!({"error": message}));
        let error = runtime.error.as_mut().expect("runtime error");
        error.error_code = Some("OFFICIAL_CODEX_APP_SERVER_FAILED".to_string());
        error.error_text = Some(message.to_string());

        assert!(
            pre_provider_commander_ledger_chain_evidence(&runtime).is_some(),
            "the old preledger validator failed before provider submission"
        );
    }

    #[test]
    fn ambiguous_or_provider_observed_failure_is_not_recoverable() {
        let baseline = pre_provider_route_admission_failure();
        let mut variants = Vec::new();

        let mut first_token = baseline.clone();
        first_token.first_token_at = Some(Utc::now());
        variants.push(first_token);

        let mut token_usage = baseline.clone();
        token_usage.context_tokens.input = 1;
        variants.push(token_usage);

        let mut assistant_text = baseline.clone();
        assistant_text.text = "provider output".to_string();
        variants.push(assistant_text);

        let mut reasoning = baseline.clone();
        reasoning.reasoning = Some("provider reasoning".to_string());
        variants.push(reasoning);

        let mut changed_output = baseline.clone();
        changed_output.output = Some(json!({"error": "different diagnostic"}));
        variants.push(changed_output);

        let mut extra_output = baseline.clone();
        extra_output.output = Some(
            json!({"error": baseline.error.as_ref().and_then(|error| error.error_text.as_deref()), "provider_result": true}),
        );
        variants.push(extra_output);

        let mut retryable = baseline;
        retryable
            .error
            .as_mut()
            .expect("runtime error")
            .retry_allowed = true;
        variants.push(retryable);

        for variant in variants {
            assert!(
                pre_provider_zero_effect_failure_evidence(&variant).is_none(),
                "provider-observed or ambiguous failure must remain fail closed"
            );
        }
    }

    fn callback_delivery() -> TerminalDeliveryIdentity {
        TerminalDeliveryIdentity {
            commander_session_id: "commander-callback".to_string(),
            transaction_id: "transaction-callback".to_string(),
            event_id: "event-callback".to_string(),
            runtime_id: "runtime-callback".to_string(),
            callback_payload_sha256: None,
            callback_effect_identity: None,
        }
    }

    fn durable_callback_fixture(
        root: &std::path::Path,
        terminal_state: TerminalState,
    ) -> (SessionLifecycleStore, TerminalDeliveryIdentity) {
        let store =
            SessionLifecycleStore::open(root, "commander-callback", LifecycleConfig::default())
                .expect("callback store");
        let receipt = callback_receipt(terminal_state);
        store
            .write_terminal_receipt(&receipt)
            .expect("terminal receipt");
        store
            .intake("transaction-callback", "event-callback")
            .expect("receipt intake");
        (store, callback_delivery())
    }

    fn admit_callback_child(
        store: &SessionLifecycleStore,
        parent_mission_revision_sha256: &str,
    ) -> ChildAdmissionRecord {
        let admission = ChildAdmissionRecord::new(
            "commander-callback",
            parent_mission_revision_sha256,
            Some("commander-thread-1".to_string()),
            "child-callback",
            "runtime-callback",
            "transaction-callback",
            "lease-callback",
            "transaction-callback",
            "runtime-callback.message",
            session_lifecycle::canonical_value_sha256(&json!("delegated prompt")),
            session_lifecycle::canonical_value_sha256(&json!({"prompt": "delegated prompt"})),
            "/tmp/child-callback",
            "delegated child callback",
            1_786_845_600_000,
        )
        .with_callback_delivery_route(
            session_lifecycle::CallbackDeliveryRoute::TrustedTuraDirectThreadWriter,
        );
        store
            .admit_child(&admission)
            .expect("durable child admission");
        admission
    }

    fn callback_ack_fixture(
        root: &std::path::Path,
        effect_identity: CallbackEffectIdentity,
        intaken: bool,
    ) -> (SessionLifecycleStore, AcknowledgeChildCallbackRequest) {
        let (store, _) = durable_callback_fixture(root, TerminalState::Completed);
        let admission = ChildAdmissionRecord::new(
            "commander-callback",
            "a".repeat(64),
            Some("commander-thread-1".to_string()),
            "child-callback",
            "runtime-callback",
            "transaction-callback",
            "lease-callback",
            "transaction-callback",
            "runtime-callback.message",
            session_lifecycle::canonical_value_sha256(&json!("delegated prompt")),
            session_lifecycle::canonical_value_sha256(&json!({"prompt": "delegated prompt"})),
            "/tmp/child-callback",
            "delegated child callback",
            1_786_845_600_000,
        )
        .with_callback_delivery_route(
            session_lifecycle::CallbackDeliveryRoute::TrustedTuraDirectThreadWriter,
        );
        store.admit_child(&admission).expect("child admission");
        let receipt = store
            .terminal_receipt("transaction-callback", "event-callback")
            .expect("terminal receipt");
        let mut callback = DurableCallbackRecord::new(
            &receipt,
            json!("child result"),
            json!({"kind": "gateway.callback"}),
            "a".repeat(64),
            session_lifecycle::canonical_value_sha256(&json!("delegated prompt")),
            effect_identity.clone(),
        )
        .expect("callback record");
        callback.commander_thread_id = Some("commander-thread-1".to_string());
        callback.callback_delivery_route =
            Some(session_lifecycle::CallbackDeliveryRoute::TrustedTuraDirectThreadWriter);
        store.publish_callback(&callback).expect("callback publish");
        if intaken {
            store
                .mark_callback_intaken(
                    &callback.transaction_id,
                    &callback.event_id,
                    &callback.callback_payload_sha256,
                )
                .expect("callback intake");
        }
        let effect_identity = match effect_identity {
            CallbackEffectIdentity::Exact { effect_id } => {
                AcknowledgeChildCallbackEffectIdentity::Exact { effect_id }
            }
            CallbackEffectIdentity::ProvenZeroEffect {
                classification,
                evidence_sha256,
            } => AcknowledgeChildCallbackEffectIdentity::ProvenZeroEffect {
                classification,
                evidence_sha256,
            },
            CallbackEffectIdentity::UnsettledEffect {
                classification,
                evidence_sha256,
            } => AcknowledgeChildCallbackEffectIdentity::UnsettledEffect {
                classification,
                evidence_sha256,
            },
        };
        let request = AcknowledgeChildCallbackRequest {
            parent_session_id: "commander-callback".to_string(),
            parent_mission_revision_sha256: "a".repeat(64),
            commander_thread_id: "commander-thread-1".to_string(),
            child_session_id: "child-callback".to_string(),
            child_runtime_id: "runtime-callback".to_string(),
            child_lease_id: "lease-callback".to_string(),
            transaction_id: "transaction-callback".to_string(),
            event_id: "event-callback".to_string(),
            callback_payload_sha256: callback.callback_payload_sha256,
            effect_identity,
        };
        (store, request)
    }

    fn begin_callback_ack_delivery(
        store: &SessionLifecycleStore,
        request: &AcknowledgeChildCallbackRequest,
    ) -> ContinuationDispatchRecord {
        let callback = store
            .intaken_callback(&request.transaction_id, &request.event_id)
            .expect("callback readback")
            .expect("intaken callback");
        let continuation =
            ContinuationDispatchRecord::from_callback(&callback).expect("continuation");
        store
            .prepare_callback_continuation(&continuation)
            .expect("prepare continuation");
        let message =
            serde_json::to_string(&continuation.parent_input).expect("serialize direct message");
        store
            .begin_callback_continuation_direct_delivery(&continuation, &message)
            .expect("begin direct delivery");
        continuation
    }

    fn reconcile_callback_ack_delivery(
        store: &SessionLifecycleStore,
        request: &AcknowledgeChildCallbackRequest,
    ) -> ContinuationDispatchRecord {
        let continuation = begin_callback_ack_delivery(store, request);
        store
            .mark_callback_continuation_delivery_reconciled(
                &continuation,
                &json!({
                    "schema_version": "test_same_thread_reconciliation_v1",
                    "request_id": continuation.request_id,
                    "target_thread_id": continuation.commander_thread_id,
                }),
            )
            .expect("reconcile direct delivery");
        continuation
    }

    fn accept_callback_ack_delivery(
        store: &SessionLifecycleStore,
        request: &AcknowledgeChildCallbackRequest,
    ) -> ContinuationDispatchRecord {
        let continuation = begin_callback_ack_delivery(store, request);
        store
            .mark_callback_continuation_delivery_accepted(
                &continuation,
                &json!({
                    "schema_version": "test_direct_delivery_acceptance_v1",
                    "request_id": continuation.request_id,
                    "accepted": true,
                }),
            )
            .expect("accept direct delivery");
        store
            .callback_continuation(&request.transaction_id, &request.event_id)
            .expect("accepted delivery readback")
            .expect("accepted delivery")
    }

    #[test]
    fn legacy_route_less_callback_cannot_acknowledge_a_ready_set_dependency() {
        let root = tempfile::tempdir().expect("callback binding root");
        let (store, _request) = callback_ack_fixture(
            root.path(),
            CallbackEffectIdentity::Exact {
                effect_id: "runtime-callback.message".to_string(),
            },
            true,
        );
        let projection = store.control_deck_projection().expect("control deck");
        let admission = projection.admissions.first().expect("admission").clone();
        let callback = projection.callbacks.first().expect("callback").clone();
        assert!(callback_matches_trusted_direct_writer_admission(
            &callback, &admission
        ));

        let mut legacy_admission = admission.clone();
        legacy_admission.callback_delivery_route = None;
        let mut legacy_callback = callback.clone();
        legacy_callback.callback_delivery_route = None;
        assert!(!callback_matches_trusted_direct_writer_admission(
            &legacy_callback,
            &legacy_admission
        ));

        let mut blank_thread_admission = admission;
        blank_thread_admission.commander_thread_id = Some("   ".to_string());
        assert!(!callback_matches_trusted_direct_writer_admission(
            &callback,
            &blank_thread_admission
        ));
    }

    #[test]
    fn public_child_callback_ack_is_exactly_once_without_provider_continuation() {
        let root = tempfile::tempdir().expect("callback ACK root");
        let (store, request) = callback_ack_fixture(
            root.path(),
            CallbackEffectIdentity::Exact {
                effect_id: "runtime-callback.message".to_string(),
            },
            true,
        );
        assert!(
            acknowledge_child_callback_from_store(&store, request.clone())
                .expect_err("ACK before a continuation exists")
                .to_string()
                .contains("CHILD_CALLBACK_ACK_CONTINUATION_NOT_FOUND")
        );
        let callback = store
            .intaken_callback(&request.transaction_id, &request.event_id)
            .expect("callback readback")
            .expect("intaken callback");
        let prepared = ContinuationDispatchRecord::from_callback(&callback).expect("continuation");
        store
            .prepare_callback_continuation(&prepared)
            .expect("prepare continuation without a delivery attempt");
        assert!(
            acknowledge_child_callback_from_store(&store, request.clone())
                .expect_err("ACK cannot confirm a merely prepared continuation")
                .to_string()
                .contains("CHILD_CALLBACK_ACK_RECONCILED_CONTINUATION_REQUIRED")
        );
        assert_eq!(
            store
                .callback_continuation(&request.transaction_id, &request.event_id)
                .expect("prepared continuation readback")
                .expect("prepared continuation")
                .state,
            session_lifecycle::ContinuationDispatchState::Prepared
        );
        begin_callback_ack_delivery(&store, &request);
        let attempted = store
            .callback_continuation(&request.transaction_id, &request.event_id)
            .expect("delivery attempt readback")
            .expect("delivery attempt");
        assert_eq!(
            attempted.state,
            session_lifecycle::ContinuationDispatchState::DeliveryUnsettled
        );
        assert!(attempted.direct_delivery_result.is_none());
        let continuation = attempted;
        let confirmation = json!({
            "schema_version": "tura_commander_ack_delivery_confirmation_v1",
            "confirmation": "exact_commander_callback_ack",
            "request_id": &continuation.request_id,
            "commander_session_id": &request.parent_session_id,
            "parent_mission_revision_sha256": &request.parent_mission_revision_sha256,
            "commander_thread_id": &request.commander_thread_id,
            "child_session_id": &request.child_session_id,
            "child_runtime_id": &request.child_runtime_id,
            "child_lease_id": &request.child_lease_id,
            "transaction_id": &request.transaction_id,
            "event_id": &request.event_id,
            "callback_payload_sha256": &request.callback_payload_sha256,
            "effect_identity": &request.effect_identity,
            "direct_delivery_call_id": &continuation.direct_delivery_call_id,
            "delivered_message_sha256": &continuation.delivered_message_sha256,
            "accepted_delivery_evidence_sha256": continuation
                .direct_delivery_result
                .as_ref()
                .map(|result| &result.evidence_sha256),
        });
        let first: AcknowledgeChildCallbackResponse = serde_json::from_value(
            acknowledge_child_callback_from_store(&store, request.clone())
                .expect("exact Commander ACK confirms the unsettled delivery attempt"),
        )
        .expect("first ACK response");
        assert_eq!(first.outcome, AcknowledgeChildCallbackOutcome::Acknowledged);
        let late_readback = json!({
            "schema_version": "test_same_thread_reconciliation_v1",
            "request_id": continuation.request_id,
            "observed_after_commander_ack": true,
        });
        assert_eq!(
            store
                .mark_callback_continuation_delivery_reconciled(&continuation, &late_readback)
                .expect("late readback proof converges after ACK confirmation"),
            session_lifecycle::ContinuationWriteOutcome::AlreadyAcknowledged
        );
        let delivered = store
            .callback_continuation(&request.transaction_id, &request.event_id)
            .expect("continuation readback")
            .expect("acknowledged continuation");
        assert_eq!(
            delivered.state,
            session_lifecycle::ContinuationDispatchState::Acknowledged
        );
        assert_eq!(
            delivered
                .direct_delivery_result
                .as_ref()
                .map(|result| result.kind),
            Some(session_lifecycle::DirectDeliveryResultKind::Reconciled)
        );
        let confirmation_sha256 = session_lifecycle::canonical_value_sha256(&confirmation);
        assert_eq!(
            delivered
                .direct_delivery_result
                .as_ref()
                .map(|result| result.evidence_sha256.as_str()),
            Some(confirmation_sha256.as_str())
        );
        let replay: AcknowledgeChildCallbackResponse = serde_json::from_value(
            acknowledge_child_callback_from_store(&store, request).expect("ACK replay"),
        )
        .expect("ACK replay response");
        assert_eq!(
            replay.outcome,
            AcknowledgeChildCallbackOutcome::AlreadyAcknowledged
        );
        assert_eq!(
            store.readback().expect("readback").acknowledged_callbacks,
            1
        );
        assert_eq!(store.readback().expect("readback").acknowledged_receipts, 1);
        assert_eq!(
            store
                .callback_continuation("transaction-callback", "event-callback")
                .expect("continuation readback")
                .expect("continuation")
                .state,
            session_lifecycle::ContinuationDispatchState::Acknowledged
        );
        assert!(store.callbacks_for_replay().expect("callbacks").is_empty());
        assert!(
            store
                .callback_continuations_for_replay()
                .expect("continuations")
                .is_empty()
        );
    }

    #[test]
    fn public_ack_after_readback_reconciliation_preserves_first_proof() {
        let root = tempfile::tempdir().expect("readback-first callback ACK root");
        let (store, request) = callback_ack_fixture(
            root.path(),
            CallbackEffectIdentity::Exact {
                effect_id: "runtime-callback.message".to_string(),
            },
            true,
        );
        let continuation = accept_callback_ack_delivery(&store, &request);
        let delivery_call_id = continuation.direct_delivery_call_id.clone();
        let delivered_message_sha256 = continuation.delivered_message_sha256.clone();
        let readback_proof = json!({
            "schema_version": "test_same_thread_reconciliation_v1",
            "request_id": continuation.request_id,
            "observed_before_commander_ack": true,
        });
        assert_eq!(
            store
                .mark_callback_continuation_delivery_reconciled(&continuation, &readback_proof)
                .expect("readback reconciles accepted delivery"),
            session_lifecycle::ContinuationWriteOutcome::AlreadyDispatched
        );
        let first: AcknowledgeChildCallbackResponse = serde_json::from_value(
            acknowledge_child_callback_from_store(&store, request.clone())
                .expect("ACK completes readback-reconciled delivery"),
        )
        .expect("first ACK response");
        let replay: AcknowledgeChildCallbackResponse = serde_json::from_value(
            acknowledge_child_callback_from_store(&store, request.clone()).expect("ACK replay"),
        )
        .expect("replayed ACK response");
        assert_eq!(first.outcome, AcknowledgeChildCallbackOutcome::Acknowledged);
        assert_eq!(
            replay.outcome,
            AcknowledgeChildCallbackOutcome::AlreadyAcknowledged
        );
        let persisted = store
            .callback_continuation(&request.transaction_id, &request.event_id)
            .expect("continuation readback")
            .expect("acknowledged continuation");
        assert_eq!(
            persisted
                .direct_delivery_result
                .as_ref()
                .expect("reconciled result")
                .evidence_sha256,
            session_lifecycle::canonical_value_sha256(&readback_proof)
        );
        assert_eq!(persisted.direct_delivery_call_id, delivery_call_id);
        assert_eq!(persisted.delivered_message_sha256, delivered_message_sha256);
        let readback = store.readback().expect("ACK counts");
        assert_eq!(readback.acknowledged_callbacks, 1);
        assert_eq!(readback.acknowledged_receipts, 1);
    }

    #[test]
    fn concurrent_identical_public_acks_have_one_linearization_winner() {
        let root = tempfile::tempdir().expect("concurrent callback ACK root");
        let (store, request) = callback_ack_fixture(
            root.path(),
            CallbackEffectIdentity::Exact {
                effect_id: "runtime-callback.message".to_string(),
            },
            true,
        );
        accept_callback_ack_delivery(&store, &request);

        let start = std::sync::Arc::new(std::sync::Barrier::new(3));
        let responses = std::thread::scope(|scope| {
            let handles = (0..2)
                .map(|_| {
                    let store = store.clone();
                    let request = request.clone();
                    let start = start.clone();
                    scope.spawn(move || {
                        start.wait();
                        serde_json::from_value::<AcknowledgeChildCallbackResponse>(
                            acknowledge_child_callback_from_store(&store, request)
                                .expect("concurrent ACK"),
                        )
                        .expect("concurrent ACK response")
                    })
                })
                .collect::<Vec<_>>();
            start.wait();
            handles
                .into_iter()
                .map(|handle| handle.join().expect("ACK thread"))
                .collect::<Vec<_>>()
        });

        assert_eq!(
            responses
                .iter()
                .filter(|response| {
                    response.outcome == AcknowledgeChildCallbackOutcome::Acknowledged
                })
                .count(),
            1
        );
        assert_eq!(
            responses
                .iter()
                .filter(|response| {
                    response.outcome == AcknowledgeChildCallbackOutcome::AlreadyAcknowledged
                })
                .count(),
            1
        );
        let readback = store.readback().expect("post-concurrency readback");
        assert_eq!(readback.acknowledged_callbacks, 1);
        assert_eq!(readback.acknowledged_receipts, 1);
        assert_eq!(
            store
                .callback_continuation(&request.transaction_id, &request.event_id)
                .expect("continuation readback")
                .expect("continuation")
                .state,
            session_lifecycle::ContinuationDispatchState::Acknowledged
        );
    }

    #[test]
    fn public_child_callback_ack_rejects_changed_bound_identities() {
        let root = tempfile::tempdir().expect("callback ACK root");
        let (store, request) = callback_ack_fixture(
            root.path(),
            CallbackEffectIdentity::Exact {
                effect_id: "runtime-callback.message".to_string(),
            },
            true,
        );
        accept_callback_ack_delivery(&store, &request);
        let mut variants = Vec::new();
        let mut changed = request.clone();
        changed.parent_session_id = "other-parent".into();
        variants.push(changed);
        let mut changed = request.clone();
        changed.child_session_id = "other-child".into();
        variants.push(changed);
        let mut changed = request.clone();
        changed.child_runtime_id = "other-runtime".into();
        variants.push(changed);
        let mut changed = request.clone();
        changed.child_lease_id = "other-lease".into();
        variants.push(changed);
        let mut changed = request.clone();
        changed.commander_thread_id = "other-thread".into();
        variants.push(changed);
        let mut changed = request.clone();
        changed.parent_mission_revision_sha256 = "b".repeat(64);
        variants.push(changed);
        let mut changed = request.clone();
        changed.transaction_id = "other-transaction".into();
        variants.push(changed);
        let mut changed = request.clone();
        changed.event_id = "other-event".into();
        variants.push(changed);
        let mut changed = request.clone();
        changed.callback_payload_sha256 = "c".repeat(64);
        variants.push(changed);
        let mut changed = request.clone();
        changed.effect_identity = AcknowledgeChildCallbackEffectIdentity::Exact {
            effect_id: "other-effect".into(),
        };
        variants.push(changed);
        for changed in variants {
            assert!(acknowledge_child_callback_from_store(&store, changed).is_err());
        }
        assert_eq!(
            store.readback().expect("readback").acknowledged_callbacks,
            0
        );
        let continuation = store
            .callback_continuation(&request.transaction_id, &request.event_id)
            .expect("continuation readback")
            .expect("delivery attempt");
        assert_eq!(
            continuation.state,
            session_lifecycle::ContinuationDispatchState::Dispatched
        );
        assert_eq!(
            continuation
                .direct_delivery_result
                .as_ref()
                .map(|result| result.kind),
            Some(session_lifecycle::DirectDeliveryResultKind::Accepted)
        );
    }

    #[test]
    fn public_child_callback_ack_requires_intaken_and_settled_effect() {
        let pending_root = tempfile::tempdir().expect("pending callback root");
        let (pending_store, pending_request) = callback_ack_fixture(
            pending_root.path(),
            CallbackEffectIdentity::Exact {
                effect_id: "runtime-callback.message".to_string(),
            },
            false,
        );
        assert!(
            acknowledge_child_callback_from_store(&pending_store, pending_request)
                .expect_err("pending callback")
                .to_string()
                .contains("INTAKEN_CALLBACK_NOT_FOUND")
        );

        let unsettled_root = tempfile::tempdir().expect("unsettled callback root");
        let (unsettled_store, unsettled_request) = callback_ack_fixture(
            unsettled_root.path(),
            CallbackEffectIdentity::UnsettledEffect {
                classification: "effect_receipt_incomplete".to_string(),
                evidence_sha256: "d".repeat(64),
            },
            true,
        );
        assert!(
            acknowledge_child_callback_from_store(&unsettled_store, unsettled_request)
                .expect_err("unsettled effect")
                .to_string()
                .contains("CALLBACK_UNSETTLED_EFFECT_ACK_BLOCKED")
        );
        assert_eq!(
            unsettled_store
                .readback()
                .expect("readback")
                .acknowledged_callbacks,
            0
        );
    }

    #[test]
    fn public_child_zero_effect_callback_ack_replays_without_effect_execution() {
        let root = tempfile::tempdir().expect("zero-effect callback root");
        let (store, request) = callback_ack_fixture(
            root.path(),
            CallbackEffectIdentity::ProvenZeroEffect {
                classification: "pre_provider_zero_effect".to_string(),
                evidence_sha256: "e".repeat(64),
            },
            true,
        );
        reconcile_callback_ack_delivery(&store, &request);
        acknowledge_child_callback_from_store(&store, request.clone()).expect("zero ACK");
        let replay: AcknowledgeChildCallbackResponse = serde_json::from_value(
            acknowledge_child_callback_from_store(&store, request).expect("zero ACK replay"),
        )
        .expect("zero replay response");
        assert_eq!(
            replay.outcome,
            AcknowledgeChildCallbackOutcome::AlreadyAcknowledged
        );
        assert!(
            store
                .callback_continuations_for_replay()
                .expect("continuations")
                .is_empty()
        );
    }

    #[test]
    fn public_child_terminal_callback_requires_admission_and_intakes_once() {
        let root = tempfile::tempdir().expect("temp lifecycle root");
        let (store, delivery) = durable_callback_fixture(root.path(), TerminalState::Completed);
        let transport_payload = json!({
            "request_id": "transaction-callback",
            "kind": "gateway.callback",
            "method": "session.agent_message",
            "payload": {
                "session_id": "child-callback",
                "runtime_id": "runtime-callback",
                "body": {
                    "type": "item.completed",
                    "item": {
                        "id": "runtime-callback.message",
                        "type": "agent_message",
                        "text": "child result"
                    }
                }
            }
        });

        let mut missing_admission_delivery = delivery.clone();
        assert_eq!(
            ExecutionService::publish_terminal_callback_from_store(
                &store,
                &mut missing_admission_delivery,
                transport_payload.clone(),
            )
            .expect_err("unadmitted child callback must fail closed")
            .to_string(),
            "TERMINAL_CALLBACK_CHILD_ADMISSION_NOT_DURABLE:child-callback"
        );
        assert_eq!(
            store
                .readback()
                .expect("pre-admission readback")
                .pending_callbacks,
            0
        );

        admit_callback_child(&store, &"a".repeat(64));

        let mut first_delivery = delivery.clone();
        let first = ExecutionService::publish_terminal_callback_from_store(
            &store,
            &mut first_delivery,
            transport_payload.clone(),
        )
        .expect("first callback publication");
        let mut replay_delivery = delivery;
        let replay = ExecutionService::publish_terminal_callback_from_store(
            &store,
            &mut replay_delivery,
            transport_payload,
        )
        .expect("identical callback replay");
        assert_eq!(replay, first);
        assert_eq!(
            first_delivery.callback_effect_identity,
            Some(CallbackEffectIdentity::Exact {
                effect_id: "runtime-callback.message".to_string(),
            })
        );
        let readback = store.readback().expect("callback intake readback");
        assert_eq!(readback.pending_callbacks, 0);
        assert_eq!(readback.intaken_callbacks, 1);
        assert_eq!(readback.acknowledged_callbacks, 0);

        let mut conflicting_delivery = first_delivery;
        let conflict = json!({
            "payload": {"body": {"item": {
                "id": "foreign.message",
                "text": "child result"
            }}}
        });
        assert!(
            ExecutionService::publish_terminal_callback_from_store(
                &store,
                &mut conflicting_delivery,
                conflict,
            )
            .expect_err("changed effect identity must conflict")
            .to_string()
            .contains("TERMINAL_CALLBACK_EFFECT_IDENTITY_CONFLICT")
        );
    }

    #[test]
    fn active_child_replay_after_forwarder_loss_converges_without_second_execution() {
        let root = tempfile::tempdir().expect("temp lifecycle root");
        let store = SessionLifecycleStore::open(
            root.path(),
            "commander-callback",
            LifecycleConfig::default(),
        )
        .expect("callback store");
        admit_callback_child(&store, &"a".repeat(64));
        assert!(
            store
                .callbacks_for_replay()
                .expect("active child callbacks")
                .is_empty()
        );

        let original_router = ExecutionService::new();
        assert!(original_router.sessions.lock().is_empty());
        drop(original_router);

        let receipt = callback_receipt(TerminalState::Completed);
        store
            .write_terminal_receipt(&receipt)
            .expect("later terminal receipt");
        store
            .intake(&receipt.transaction_id, &receipt.event_id)
            .expect("later terminal intake");
        let transport = json!({
            "request_id": "transaction-callback",
            "kind": "gateway.callback",
            "method": "session.agent_message",
            "payload": {"body": {"item": {
                "id": "runtime-callback.message",
                "text": "child result"
            }}}
        });
        let mut first_delivery = callback_delivery();
        ExecutionService::publish_terminal_callback_from_store(
            &store,
            &mut first_delivery,
            transport.clone(),
        )
        .expect("replacement forwarder terminal publication");
        let mut replay_delivery = callback_delivery();
        ExecutionService::publish_terminal_callback_from_store(
            &store,
            &mut replay_delivery,
            transport,
        )
        .expect("identical forwarder replay");

        let callbacks = store
            .callbacks_for_replay()
            .expect("single callback replay");
        assert_eq!(callbacks.len(), 1);
        let continuation =
            ContinuationDispatchRecord::from_callback(&callbacks[0]).expect("continuation");
        store
            .prepare_callback_continuation(&continuation)
            .expect("prepare continuation");
        accept_test_direct_delivery(&store, &continuation);
        let continuation = bind_test_convergence_proof(&store, &continuation);
        store
            .mark_callback_continuation_completed(&continuation)
            .expect("complete continuation");
        complete_and_ack_callback_continuation(&store, &continuation).expect("first ack");
        complete_and_ack_callback_continuation(&store, &continuation).expect("replayed ack");

        let restarted_router = ExecutionService::new();
        assert!(restarted_router.sessions.lock().is_empty());
        let readback = store.readback().expect("terminal convergence readback");
        assert_eq!(readback.intaken_callbacks, 1);
        assert_eq!(readback.acknowledged_callbacks, 1);
        assert_eq!(readback.acknowledged_receipts, 1);
        assert!(
            store
                .callbacks_for_replay()
                .expect("post-ack callbacks")
                .is_empty()
        );
        assert!(
            store
                .callback_continuations_for_replay()
                .expect("post-ack continuations")
                .is_empty()
        );
    }

    #[test]
    fn fresh_turn_and_router_restart_replay_without_in_memory_lease() {
        let root = tempfile::tempdir().expect("temp lifecycle root");
        let (store, delivery) = durable_callback_fixture(root.path(), TerminalState::Completed);
        admit_callback_child(&store, &"a".repeat(64));
        let record = DurableCallbackRecord::new(
            &store
                .terminal_receipt(&delivery.transaction_id, &delivery.event_id)
                .expect("receipt"),
            json!("child result"),
            json!({"kind": "gateway.callback", "payload": {"body": {"item": {"id": "message-1", "text": "child result"}}}}),
            "a".repeat(64),
            session_lifecycle::canonical_value_sha256(&json!("delegated prompt")),
            CallbackEffectIdentity::Exact {
                effect_id: "message-1".to_string(),
            },
        )
        .expect("callback");
        let record = bind_direct_writer_callback(record);
        store.publish_callback(&record).expect("pending callback");
        let fresh_service = ExecutionService::new();
        assert!(fresh_service.sessions.lock().is_empty());

        let first = replay_terminal_callbacks_from_store(
            &store,
            "commander-callback",
            "child-callback",
            "transaction-callback",
        )
        .expect("fresh replay before lease");
        assert_eq!(first.len(), 1);
        let readback = store.readback().expect("durable intake readback");
        assert_eq!(readback.pending_callbacks, 0);
        assert_eq!(readback.intaken_callbacks, 1);
        assert_eq!(readback.acknowledged_callbacks, 0);

        let restarted_service = ExecutionService::new();
        assert!(restarted_service.sessions.lock().is_empty());
        let duplicate = replay_terminal_callbacks_from_store(
            &store,
            "commander-callback",
            "child-callback",
            "transaction-callback",
        )
        .expect("restart replay without lease");
        assert_eq!(duplicate, first);
        assert_eq!(store.readback().expect("readback").intaken_callbacks, 1);
    }

    #[test]
    fn callback_continuation_completes_and_acks_exactly_once() {
        let root = tempfile::tempdir().expect("temp lifecycle root");
        let (store, delivery) = durable_callback_fixture(root.path(), TerminalState::Completed);
        admit_callback_child(&store, &"a".repeat(64));
        let callback = DurableCallbackRecord::new(
            &store
                .terminal_receipt(&delivery.transaction_id, &delivery.event_id)
                .expect("receipt"),
            json!("child result"),
            json!({"kind": "gateway.callback", "payload": {"body": {"item": {"id": "message-1", "text": "child result"}}}}),
            "a".repeat(64),
            session_lifecycle::canonical_value_sha256(&json!("delegated prompt")),
            CallbackEffectIdentity::Exact {
                effect_id: "message-1".to_string(),
            },
        )
        .expect("callback");
        let callback = bind_direct_writer_callback(callback);
        store.publish_callback(&callback).expect("publish callback");
        store
            .mark_callback_intaken(
                &callback.transaction_id,
                &callback.event_id,
                &callback.callback_payload_sha256,
            )
            .expect("intake callback");
        let continuation =
            ContinuationDispatchRecord::from_callback(&callback).expect("continuation");
        store
            .prepare_callback_continuation(&continuation)
            .expect("prepare continuation");

        accept_test_direct_delivery(&store, &continuation);
        let bound = bind_test_convergence_proof(&store, &continuation);
        complete_and_ack_callback_continuation(&store, &bound).expect("first completion");
        complete_and_ack_callback_continuation(&store, &bound).expect("duplicate replay");

        let readback = store.readback().expect("readback");
        assert_eq!(readback.acknowledged_callbacks, 1);
        assert_eq!(readback.acknowledged_receipts, 1);
        assert!(
            store
                .callbacks_for_replay()
                .expect("callback replay")
                .is_empty()
        );
        assert!(
            store
                .callback_continuations_for_replay()
                .expect("continuation replay")
                .is_empty()
        );
        assert_eq!(
            store
                .prepare_callback_continuation(&continuation)
                .expect("duplicate successful replay"),
            session_lifecycle::ContinuationWriteOutcome::AlreadyAcknowledged
        );
    }

    #[test]
    fn commander_convergence_runtime_output_must_bind_exact_continuation() {
        let receipt = callback_receipt(TerminalState::Completed);
        let callback = DurableCallbackRecord::new(
            &receipt,
            json!("child result"),
            json!({"kind": "gateway.callback", "payload": {"body": {"item": {"id": "message-1", "text": "child result"}}}}),
            "a".repeat(64),
            session_lifecycle::canonical_value_sha256(&json!("delegated prompt")),
            CallbackEffectIdentity::Exact {
                effect_id: "message-1".to_string(),
            },
        )
        .expect("callback");
        let callback = bind_direct_writer_callback(callback);
        let record =
            ContinuationDispatchRecord::from_callback(&callback).expect("target continuation");
        let final_content = json!("commander converged");
        let effect = serde_json::to_value(&record.effect_identity).expect("effect value");
        let proof = CommanderConvergenceProof {
            schema_version: runtime_contract::COMMANDER_CONVERGENCE_PROOF_SCHEMA_VERSION
                .to_string(),
            request_id: record.request_id.clone(),
            callback_payload_sha256: record.callback_payload_sha256.clone(),
            effect_identity_sha256: session_lifecycle::canonical_value_sha256(&effect),
            child_session_id: record.child_session_id.clone(),
            child_transaction_id: record.child_transaction_id.clone(),
            child_runtime_id: record.child_runtime_id.clone(),
            requested_action: record.requested_action.clone(),
            target_thread_id: record
                .commander_thread_id
                .clone()
                .expect("commander target"),
            pre_revision_sha256: record.parent_mission_revision_sha256.clone(),
            post_revision_sha256: "b".repeat(64),
            target_turn_id: "turn-commander-1".to_string(),
            final_assistant_sha256: session_lifecycle::canonical_value_sha256(&final_content),
        };
        let mut runtime = RuntimeAggregate::new(
            record.runtime_id.clone(),
            record.commander_session_id.clone(),
            "continuation-agent".to_string(),
            RuntimeProviderConfig {
                base: ProviderConfig {
                    tura_llm_name: "official_codex_app_server".to_string(),
                    default_model_tier: None,
                    current_model: Some("test-model".to_string()),
                    stream: true,
                    temperature: 0.0,
                    max_tokens: 256,
                    tool_choice: ToolChoice::Auto,
                    time_out_ms: 1_000,
                },
                thinking: false,
                provider_name: "official_codex_app_server".to_string(),
                model_name: "test-model".to_string(),
                provider_url_name: "local".to_string(),
                llm_provider_name: "openai".to_string(),
            },
            Utc::now(),
        );
        runtime
            .mark_called(runtime.created_at)
            .expect("runtime called");
        runtime
            .mark_waiting_first_token()
            .expect("runtime waiting first token");
        runtime
            .mark_first_token(runtime.created_at)
            .expect("runtime first token");
        runtime
            .set_output(json!({
                "content": final_content,
                "commander_convergence_proof": proof,
            }))
            .expect("capture provider output");
        runtime
            .finish_success(runtime.created_at, None)
            .expect("finish runtime");

        assert_eq!(
            commander_convergence_proof_from_runtime(&record, &runtime)
                .expect("exact proof replay"),
            proof
        );

        let mut fallback = runtime.clone();
        fallback.runtime_id = "callback-continuation-recovery-runtime-test".to_string();
        fallback.fallback_from_id = Some(record.runtime_id.clone());
        assert_eq!(
            commander_convergence_proof_from_runtime(&record, &fallback)
                .expect("exact fallback proof replay"),
            proof
        );
        let mut bounded_retry = fallback.clone();
        bounded_retry.runtime_id =
            "callback-continuation-recovery-runtime-bounded-retry".to_string();
        bounded_retry.fallback_from_id = Some(fallback.runtime_id.clone());
        assert_eq!(
            commander_convergence_proof_from_runtime(&record, &bounded_retry)
                .expect("bounded continuation retry proof replay"),
            proof
        );
        fallback.fallback_from_id = Some("unrelated-runtime".to_string());
        assert!(
            commander_convergence_proof_from_runtime(&record, &fallback)
                .expect_err("unbound fallback must fail")
                .to_string()
                .contains("COMMANDER_CONVERGENCE_RUNTIME_IDENTITY_MISMATCH")
        );

        let mut wrong_final = runtime;
        wrong_final
            .output
            .as_mut()
            .and_then(|output| output.get_mut("content"))
            .expect("final content")
            .clone_from(&json!("different final"));
        assert!(
            commander_convergence_proof_from_runtime(&record, &wrong_final)
                .expect_err("final answer hash mismatch must fail at Router replay")
                .to_string()
                .contains("COMMANDER_CONVERGENCE_FINAL_ASSISTANT_HASH_MISMATCH")
        );
    }

    #[test]
    fn startup_convergence_evidence_distinguishes_absence_from_loader_errors() {
        let receipt = callback_receipt(TerminalState::Completed);
        let callback = DurableCallbackRecord::new(
            &receipt,
            json!("child result"),
            json!({"kind": "gateway.callback"}),
            "a".repeat(64),
            session_lifecycle::canonical_value_sha256(&json!("delegated prompt")),
            CallbackEffectIdentity::Exact {
                effect_id: "message-1".to_string(),
            },
        )
        .expect("callback");
        let callback = bind_direct_writer_callback(callback);
        let record = ContinuationDispatchRecord::from_callback(&callback).expect("continuation");
        let binding = commander_continuation_binding(&record).expect("binding");

        let absent = tempfile::tempdir().expect("absent ledger root");
        assert!(
            terminal_commander_convergence_recovery_evidence(
                absent.path(),
                &record.commander_session_id,
                &record.runtime_id,
                &binding,
            )
            .expect("true absence")
            .is_none()
        );

        let malformed = tempfile::tempdir().expect("malformed ledger root");
        let ledger_root = malformed.path().join(".tura/run/effect_ledgers");
        std::fs::create_dir_all(&ledger_root).expect("ledger directory");
        std::fs::write(ledger_root.join("malformed.json"), b"{").expect("malformed ledger");
        assert!(
            terminal_commander_convergence_recovery_evidence(
                malformed.path(),
                &record.commander_session_id,
                &record.runtime_id,
                &binding,
            )
            .is_err()
        );

        let overfull = tempfile::tempdir().expect("overfull ledger root");
        let ledger_root = overfull.path().join(".tura/run/effect_ledgers");
        std::fs::create_dir_all(&ledger_root).expect("ledger directory");
        for index in 0..257 {
            std::fs::write(ledger_root.join(format!("{index:03}.json")), b"{}").expect("ledger");
        }
        assert!(
            terminal_commander_convergence_recovery_evidence(
                overfull.path(),
                &record.commander_session_id,
                &record.runtime_id,
                &binding,
            )
            .is_err()
        );
    }

    struct RecoveryTestEnvGuard {
        session_db: crate::services::session_db::SessionDbService,
        previous_home: Option<std::ffi::OsString>,
        previous_db: Option<std::ffi::OsString>,
        previous_project: Option<std::ffi::OsString>,
        previous_release_bin: Option<std::ffi::OsString>,
    }

    impl RecoveryTestEnvGuard {
        fn install(
            session_db: crate::services::session_db::SessionDbService,
            db_root: &std::path::Path,
            project_root: &std::path::Path,
        ) -> Self {
            let guard = Self {
                session_db,
                previous_home: std::env::var_os("TURA_HOME"),
                previous_db: std::env::var_os("SESSION_LOG_DB_ROOT"),
                previous_project: std::env::var_os("TURA_PROJECT_ROOT"),
                previous_release_bin: std::env::var_os("TURA_RELEASE_BIN_DIR"),
            };
            #[allow(unsafe_code)]
            unsafe {
                std::env::set_var("TURA_HOME", db_root);
                std::env::set_var("SESSION_LOG_DB_ROOT", db_root);
                std::env::set_var("TURA_PROJECT_ROOT", project_root);
                std::env::set_var("TURA_RELEASE_BIN_DIR", db_root);
            }
            guard
        }
    }

    impl Drop for RecoveryTestEnvGuard {
        fn drop(&mut self) {
            self.session_db.shutdown();
            #[allow(unsafe_code)]
            unsafe {
                match self.previous_home.take() {
                    Some(value) => std::env::set_var("TURA_HOME", value),
                    None => std::env::remove_var("TURA_HOME"),
                }
                match self.previous_db.take() {
                    Some(value) => std::env::set_var("SESSION_LOG_DB_ROOT", value),
                    None => std::env::remove_var("SESSION_LOG_DB_ROOT"),
                }
                match self.previous_project.take() {
                    Some(value) => std::env::set_var("TURA_PROJECT_ROOT", value),
                    None => std::env::remove_var("TURA_PROJECT_ROOT"),
                }
                match self.previous_release_bin.take() {
                    Some(value) => std::env::set_var("TURA_RELEASE_BIN_DIR", value),
                    None => std::env::remove_var("TURA_RELEASE_BIN_DIR"),
                }
            }
        }
    }

    struct InProcessSessionDbGuard {
        handle: Option<std::thread::JoinHandle<anyhow::Result<()>>>,
    }

    impl InProcessSessionDbGuard {
        fn start() -> Self {
            let (ready_tx, ready_rx) = std::sync::mpsc::sync_channel(1);
            let handle = std::thread::spawn(move || {
                session_log::service::run_socket_service_with_ready(|| {
                    ready_tx.send(()).expect("publish session DB readiness");
                })
            });
            if let Err(error) = ready_rx.recv_timeout(Duration::from_secs(5)) {
                let detail = match handle.join() {
                    Ok(Ok(())) => "service exited before readiness".to_string(),
                    Ok(Err(error)) => error.to_string(),
                    Err(_) => "service thread panicked before readiness".to_string(),
                };
                panic!("in-process session DB did not publish readiness: {error}: {detail}");
            }
            Self {
                handle: Some(handle),
            }
        }
    }

    impl Drop for InProcessSessionDbGuard {
        fn drop(&mut self) {
            let shutdown = session_log_contract::client::call_service(
                &session_log_contract::SessionLogCommand::Shutdown,
            );
            let joined = self.handle.take().map(std::thread::JoinHandle::join);
            if !std::thread::panicking() {
                shutdown.expect("shutdown in-process session DB");
                joined
                    .expect("in-process session DB thread")
                    .expect("join in-process session DB thread")
                    .expect("run in-process session DB service");
            }
        }
    }

    #[test]
    fn recovery_test_env_guard_restores_shared_env_during_unwind() {
        let _lock = crate::services::ROUTER_TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let previous_home = std::env::var_os("TURA_HOME");
        let previous_db = std::env::var_os("SESSION_LOG_DB_ROOT");
        let previous_project = std::env::var_os("TURA_PROJECT_ROOT");
        let root = tempfile::tempdir().expect("env guard root");
        let result = std::panic::catch_unwind(|| {
            let _env = RecoveryTestEnvGuard::install(
                crate::services::session_db::SessionDbService::new(),
                root.path(),
                root.path(),
            );
            panic!("injected assertion unwind");
        });
        assert!(result.is_err());
        assert_eq!(std::env::var_os("TURA_HOME"), previous_home);
        assert_eq!(std::env::var_os("SESSION_LOG_DB_ROOT"), previous_db);
        assert_eq!(std::env::var_os("TURA_PROJECT_ROOT"), previous_project);
    }

    #[tokio::test]
    async fn malformed_convergence_ledger_callsite_has_zero_durable_mutation() {
        let _lock = crate::services::ROUTER_TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let root = tempfile::tempdir().expect("isolated recovery root");
        let project = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(std::path::Path::parent)
            .expect("project root");
        let state = build_state();
        let _env = RecoveryTestEnvGuard::install(state.session_db.clone(), root.path(), project);
        state.session_db.start().expect("isolated session db");
        let session_id = "commander-callback";
        let directory = root.path().join("workspace");
        std::fs::create_dir_all(&directory).expect("workspace");
        let created = session_log_contract::client::call_service(
            &session_log_contract::SessionLogCommand::CreateSession(Box::new(
                session_log_contract::CreateSessionRequest {
                    command_id: "create-callsite".into(),
                    session_id: session_id.into(),
                    creation_command: lifecycle::SessionCommand::CreateSession {
                        task_plan: lifecycle::TaskPlan::default(),
                    },
                    copy_context: false,
                    workspace: directory.display().to_string(),
                    session_directory: directory.display().to_string(),
                    name: "callsite".into(),
                    created_at: 1,
                    model: None,
                    agent: None,
                    session_type: "coding".into(),
                    kill_processes_on_start: false,
                    validator_enabled: false,
                    force_planning: false,
                    model_variant: None,
                    model_acceleration_enabled: false,
                    disable_permission_restrictions: true,
                    use_last_tool_call_response: false,
                    auto_session_name: false,
                    initial_task_plan_patch: None,
                },
            )),
        )
        .expect("create session");
        assert!(matches!(
            created,
            session_log_contract::SessionLogResponse::SessionCommandApplied { .. }
        ));

        let receipt = callback_receipt(TerminalState::Completed);
        let callback = DurableCallbackRecord::new(
            &receipt,
            json!("child result"),
            json!({"kind": "gateway.callback"}),
            "a".repeat(64),
            session_lifecycle::canonical_value_sha256(&json!("delegated prompt")),
            CallbackEffectIdentity::Exact {
                effect_id: "message-1".into(),
            },
        )
        .expect("callback");
        let callback = bind_direct_writer_callback(callback);
        let continuation =
            ContinuationDispatchRecord::from_callback(&callback).expect("continuation");
        let runtime_id = continuation.runtime_id.clone();
        let lease_id = continuation.lease_id.clone();
        let store = lifecycle_store(session_id).expect("lifecycle store");
        store.write_terminal_receipt(&receipt).expect("receipt");
        store
            .intake(&receipt.transaction_id, &receipt.event_id)
            .expect("receipt intake");
        store.publish_callback(&callback).expect("callback publish");
        store
            .mark_callback_intaken(
                &callback.transaction_id,
                &callback.event_id,
                &callback.callback_payload_sha256,
            )
            .expect("callback intake");
        store
            .prepare_callback_continuation(&continuation)
            .expect("prepare continuation");
        accept_test_direct_delivery(&store, &continuation);
        register_and_activate_runtime(
            session_id,
            &runtime_id,
            &lease_id,
            None,
            Some(RuntimeLifecycleIdentity {
                commander_session_id: session_id.into(),
                transaction_id: continuation.request_id.clone(),
                parent_mission_revision_sha256: Some(
                    continuation.parent_mission_revision_sha256.clone(),
                ),
                delegated_input_sha256: Some(continuation.delegated_input_sha256.clone()),
                task_id: None,
                goal_id: None,
                operator_override: false,
                dispatch_runtime_id: runtime_id.clone(),
                dispatch_lease_id: lease_id.clone(),
                receipt_event_seq: 0,
            }),
        )
        .expect("register runtime");
        let snapshot = match session_log_contract::client::call_service(
            &session_log_contract::SessionLogCommand::GetRuntimeLease(
                session_log_contract::GetRuntimeLeaseRequest {
                    runtime_id: runtime_id.clone(),
                    database_path: None,
                },
            ),
        )
        .expect("lease")
        {
            session_log_contract::SessionLogResponse::RuntimeLeaseRead {
                runtime: Some(runtime),
            } => runtime,
            other => panic!("unexpected lease {other:?}"),
        };
        let before_runtime = serde_json::to_value(
            session_log_contract::client::call_service(
                &session_log_contract::SessionLogCommand::ReplayRuntime(
                    session_log_contract::ReplayRuntimeRequest {
                        runtime_id: runtime_id.clone(),
                    },
                ),
            )
            .expect("runtime before"),
        )
        .expect("serialize runtime");
        let before_session = read_session_snapshot(session_id).expect("session before");
        let lifecycle_before = store.readback().expect("lifecycle before");
        let ledgers = directory.join(".tura/run/effect_ledgers");
        std::fs::create_dir_all(&ledgers).expect("ledgers");
        std::fs::write(ledgers.join("malformed.json"), b"{").expect("malformed ledger");
        let error = ExecutionService::new().recovery_close_runtime(&state, json!({
            "receipt_id": "callsite-recovery", "database_path": snapshot.database_path,
            "runtime_id": runtime_id.clone(), "session_id": session_id, "lease_id": lease_id,
            "expected_lease_active": true, "expected_revision": snapshot.revision,
            "expected_last_event_seq": snapshot.last_event_seq,
            "expected_session_event_seq": snapshot.session_event_seq,
            "expected_session_state": snapshot.session_state, "reason": "orphaned_runtime"
        })).await.expect_err("malformed evidence must fail closed");
        assert!(
            error.to_string().contains("invalid execution ledger"),
            "{error}"
        );
        let after_runtime = serde_json::to_value(
            session_log_contract::client::call_service(
                &session_log_contract::SessionLogCommand::ReplayRuntime(
                    session_log_contract::ReplayRuntimeRequest { runtime_id },
                ),
            )
            .expect("runtime after"),
        )
        .expect("serialize runtime");
        assert_eq!(before_runtime, after_runtime);
        assert_eq!(
            before_session,
            read_session_snapshot(session_id).expect("session after")
        );
        assert_eq!(lifecycle_before, store.readback().expect("lifecycle after"));
    }

    #[test]
    fn registered_parent_failure_remains_dispatched_and_unacknowledged() {
        let root = tempfile::tempdir().expect("temp lifecycle root");
        let (store, delivery) = durable_callback_fixture(root.path(), TerminalState::Completed);
        admit_callback_child(&store, &"a".repeat(64));
        let callback = DurableCallbackRecord::new(
            &store
                .terminal_receipt(&delivery.transaction_id, &delivery.event_id)
                .expect("receipt"),
            json!("child result"),
            json!({"kind": "gateway.callback", "payload": {"body": {"item": {"id": "message-1", "text": "child result"}}}}),
            "a".repeat(64),
            session_lifecycle::canonical_value_sha256(&json!("delegated prompt")),
            CallbackEffectIdentity::Exact {
                effect_id: "message-1".to_string(),
            },
        )
        .expect("callback");
        let callback = bind_direct_writer_callback(callback);
        store.publish_callback(&callback).expect("publish callback");
        store
            .mark_callback_intaken(
                &callback.transaction_id,
                &callback.event_id,
                &callback.callback_payload_sha256,
            )
            .expect("intake callback");
        let continuation =
            ContinuationDispatchRecord::from_callback(&callback).expect("continuation");
        store
            .prepare_callback_continuation(&continuation)
            .expect("prepare continuation");
        assert_eq!(
            store
                .callback_continuations_for_replay()
                .expect("pre-registration continuation")[0]
                .state,
            session_lifecycle::ContinuationDispatchState::Prepared
        );
        assert_eq!(
            store
                .readback()
                .expect("registration failure readback")
                .acknowledged_callbacks,
            0
        );
        accept_test_direct_delivery(&store, &continuation);
        assert_eq!(
            require_successful_runtime_dispatch(
                500,
                &json!({"result": {"error": "provider failed"}}),
            )
            .expect_err("provider failure must stop before completion")
            .to_string(),
            "provider failed"
        );

        let readback = store.readback().expect("failure readback");
        assert_eq!(readback.acknowledged_callbacks, 0);
        assert_eq!(readback.acknowledged_receipts, 0);
        assert_eq!(
            store
                .callback_continuations_for_replay()
                .expect("uncertain continuation")[0]
                .state,
            session_lifecycle::ContinuationDispatchState::Dispatched
        );
    }

    #[tokio::test]
    async fn callback_reconciled_restart_awaits_explicit_ack_without_second_execution() {
        let commander_session_id = format!("commander-callback-{}", uuid::Uuid::new_v4());
        let default_store_path = commander_store_path(
            &session_log_contract::client::default_db_dir().join("session_lifecycle_v1"),
            &commander_session_id,
        )
        .expect("default callback store path");
        assert!(
            !default_store_path.exists(),
            "test identity must not preexist in the default lifecycle root"
        );
        let root = tempfile::tempdir().expect("temp lifecycle root");
        let store = SessionLifecycleStore::open(
            root.path(),
            &commander_session_id,
            LifecycleConfig::default(),
        )
        .expect("callback store");
        let mut receipt = TerminalReceipt::new(
            TerminalReceiptIdentity::new(
                "transaction-callback",
                "event-callback",
                0,
                &commander_session_id,
                "child-callback",
                "runtime-callback",
                "lease-callback",
            ),
            TerminalState::Completed,
            1_786_845_600_000,
        );
        receipt.audit_metadata.insert(
            "parent_mission_revision_sha256".to_string(),
            json!("a".repeat(64)),
        );
        receipt.audit_metadata.insert(
            "delegated_input_sha256".to_string(),
            json!(session_lifecycle::canonical_value_sha256(&json!(
                "delegated prompt"
            ))),
        );
        store
            .write_terminal_receipt(&receipt)
            .expect("terminal receipt");
        store
            .intake("transaction-callback", "event-callback")
            .expect("receipt intake");
        let admission = ChildAdmissionRecord::new(
            &commander_session_id,
            "a".repeat(64),
            Some("commander-thread-1".to_string()),
            "child-callback",
            "runtime-callback",
            "transaction-callback",
            "lease-callback",
            "transaction-callback",
            "runtime-callback.message",
            session_lifecycle::canonical_value_sha256(&json!("delegated prompt")),
            session_lifecycle::canonical_value_sha256(&json!({"prompt": "delegated prompt"})),
            "/tmp/child-callback",
            "delegated child callback",
            1_786_845_600_000,
        )
        .with_callback_delivery_route(
            session_lifecycle::CallbackDeliveryRoute::TrustedTuraDirectThreadWriter,
        );
        store.admit_child(&admission).expect("child admission");
        let callback = DurableCallbackRecord::new(
            &receipt,
            json!("child result"),
            json!({"kind": "gateway.callback", "payload": {"body": {"item": {"id": "message-1", "text": "child result"}}}}),
            "a".repeat(64),
            session_lifecycle::canonical_value_sha256(&json!("delegated prompt")),
            CallbackEffectIdentity::Exact {
                effect_id: "runtime-callback.message".to_string(),
            },
        )
        .expect("callback");
        let callback = bind_direct_writer_callback(callback);
        store.publish_callback(&callback).expect("publish callback");
        store
            .mark_callback_intaken(
                &callback.transaction_id,
                &callback.event_id,
                &callback.callback_payload_sha256,
            )
            .expect("intake callback");
        let continuation =
            ContinuationDispatchRecord::from_callback(&callback).expect("continuation");
        store
            .prepare_callback_continuation(&continuation)
            .expect("prepare continuation");
        reconcile_test_direct_delivery(&store, &continuation);
        let delivery = TerminalDeliveryIdentity {
            commander_session_id: commander_session_id.clone(),
            transaction_id: callback.transaction_id.clone(),
            event_id: callback.event_id.clone(),
            runtime_id: callback.runtime_id.clone(),
            callback_payload_sha256: Some(callback.callback_payload_sha256.clone()),
            callback_effect_identity: Some(callback.effect_identity.clone()),
        };
        drop(store);
        let store = SessionLifecycleStore::open(
            root.path(),
            &commander_session_id,
            LifecycleConfig::default(),
        )
        .expect("reopen callback store after restart");

        let state = build_state();
        let service = ExecutionService::new();
        for (field, changed) in [
            (
                "transaction",
                TerminalDeliveryIdentity {
                    transaction_id: "changed-transaction".to_string(),
                    ..delivery.clone()
                },
            ),
            (
                "event",
                TerminalDeliveryIdentity {
                    event_id: "changed-event".to_string(),
                    ..delivery.clone()
                },
            ),
            (
                "runtime",
                TerminalDeliveryIdentity {
                    runtime_id: "changed-runtime".to_string(),
                    ..delivery.clone()
                },
            ),
            (
                "payload",
                TerminalDeliveryIdentity {
                    callback_payload_sha256: Some("b".repeat(64)),
                    ..delivery.clone()
                },
            ),
            (
                "effect",
                TerminalDeliveryIdentity {
                    callback_effect_identity: Some(CallbackEffectIdentity::Exact {
                        effect_id: "changed-effect".to_string(),
                    }),
                    ..delivery.clone()
                },
            ),
        ] {
            let error = service
                .continue_terminal_delivery_with_store(&state, &changed, &store)
                .await
                .expect_err("changed recovery identity must fail closed");
            assert!(
                error
                    .to_string()
                    .starts_with(if field == "transaction" || field == "event" {
                        "PERSISTED_CALLBACK_FOR_CONTINUATION_NOT_FOUND:"
                    } else {
                        "CONTINUATION_DELIVERY_IDENTITY_MISMATCH:"
                    }),
                "unexpected {field} identity error: {error}"
            );
        }
        assert_eq!(
            service.sessions.lock().len(),
            0,
            "enqueue count before recovery"
        );

        let result = service
            .continue_terminal_delivery_with_store(&state, &delivery, &store)
            .await
            .expect("restart recognizes reconciled delivery without another send");
        assert_eq!(
            result["status"],
            "direct_delivery_reconciled_awaiting_commander_ack"
        );
        assert_eq!(
            service.sessions.lock().len(),
            0,
            "provider/enqueue execution delta"
        );
        let pre_ack = store.readback().expect("pre-ACK readback");
        assert_eq!(pre_ack.acknowledged_callbacks, 0);
        assert_eq!(pre_ack.acknowledged_receipts, 0);
        assert_eq!(
            store
                .callback_continuation(&callback.transaction_id, &callback.event_id)
                .expect("continuation readback")
                .expect("continuation")
                .state,
            session_lifecycle::ContinuationDispatchState::Dispatched
        );

        let ack_request = AcknowledgeChildCallbackRequest {
            parent_session_id: commander_session_id.clone(),
            parent_mission_revision_sha256: "a".repeat(64),
            commander_thread_id: "commander-thread-1".to_string(),
            child_session_id: callback.child_session_id.clone(),
            child_runtime_id: callback.runtime_id.clone(),
            child_lease_id: callback.lease_id.clone(),
            transaction_id: callback.transaction_id.clone(),
            event_id: callback.event_id.clone(),
            callback_payload_sha256: callback.callback_payload_sha256.clone(),
            effect_identity: AcknowledgeChildCallbackEffectIdentity::Exact {
                effect_id: "runtime-callback.message".to_string(),
            },
        };
        let first_ack: AcknowledgeChildCallbackResponse = serde_json::from_value(
            acknowledge_child_callback_from_store(&store, ack_request.clone())
                .expect("explicit Commander ACK"),
        )
        .expect("ACK response");
        assert_eq!(
            first_ack.outcome,
            AcknowledgeChildCallbackOutcome::Acknowledged
        );
        let replayed_ack: AcknowledgeChildCallbackResponse = serde_json::from_value(
            acknowledge_child_callback_from_store(&store, ack_request)
                .expect("idempotent Commander ACK"),
        )
        .expect("replayed ACK response");
        assert_eq!(
            replayed_ack.outcome,
            AcknowledgeChildCallbackOutcome::AlreadyAcknowledged
        );

        drop(store);
        let reopened = SessionLifecycleStore::open(
            root.path(),
            &commander_session_id,
            LifecycleConfig::default(),
        )
        .expect("reopen after recovery");
        let readback = reopened.readback().expect("readback");
        assert_eq!(readback.acknowledged_callbacks, 1);
        assert_eq!(readback.acknowledged_receipts, 1);
        assert!(
            reopened
                .callbacks_for_replay()
                .expect("callback replay")
                .is_empty()
        );
        assert!(
            reopened
                .callback_continuations_for_replay()
                .expect("continuation replay")
                .is_empty()
        );
        assert_eq!(
            reopened
                .prepare_callback_continuation(&continuation)
                .expect("acknowledged continuation readback"),
            session_lifecycle::ContinuationWriteOutcome::AlreadyAcknowledged
        );
        assert!(
            service
                .recover_callback_continuations(&state, &commander_session_id)
                .await
                .expect("formal replay after recovery")
                .is_empty()
        );
        assert_eq!(service.sessions.lock().len(), 0, "final enqueue count");
        assert!(
            !default_store_path.exists(),
            "test must not create its identity in the default lifecycle root"
        );
    }

    #[test]
    fn missing_commander_store_is_an_empty_replay() {
        let service = ExecutionService::new();
        let commander_session_id = format!("missing-commander-{}", uuid::Uuid::new_v4());
        let replay = service
            .replay_terminal_callbacks(
                &commander_session_id,
                "missing-child",
                "missing-transaction",
            )
            .expect("missing store is not fatal");
        assert!(replay.is_empty());
        assert!(service.sessions.lock().is_empty());
    }

    #[test]
    fn replay_changed_immutable_identity_conflicts() {
        let root = tempfile::tempdir().expect("temp lifecycle root");
        let (store, delivery) = durable_callback_fixture(root.path(), TerminalState::Completed);
        admit_callback_child(&store, &"a".repeat(64));
        let record = DurableCallbackRecord::new(
            &store
                .terminal_receipt(&delivery.transaction_id, &delivery.event_id)
                .expect("receipt"),
            json!("child result"),
            json!({"payload": {"body": {"item": {"id": "message-1", "text": "child result"}}}}),
            "a".repeat(64),
            session_lifecycle::canonical_value_sha256(&json!("delegated prompt")),
            CallbackEffectIdentity::Exact {
                effect_id: "message-1".to_string(),
            },
        )
        .expect("callback");
        let record = bind_direct_writer_callback(record);
        store.publish_callback(&record).expect("pending callback");

        let error = replay_terminal_callbacks_from_store(
            &store,
            "commander-callback",
            "different-child",
            "transaction-callback",
        )
        .expect_err("changed child identity must fail closed");
        assert!(
            error
                .to_string()
                .starts_with("CALLBACK_REPLAY_IDENTITY_MISMATCH:")
        );
    }

    #[test]
    fn delegated_prompt_digest_is_recomputed_and_mismatch_rejected() {
        let mut request = RunAgentRequest {
            prompt: Some("authoritative prompt".to_string()),
            message: Some("ignored message".to_string()),
            delegated_input_sha256: Some("b".repeat(64)),
            ..Default::default()
        };
        let error = validate_delegated_input_digest(&mut request, true)
            .expect_err("caller digest mismatch");
        assert!(
            error
                .to_string()
                .starts_with("DELEGATED_INPUT_SHA256_MISMATCH:")
        );

        request.delegated_input_sha256 = request.effective_prompt_sha256();
        validate_delegated_input_digest(&mut request, true).expect("matching digest");
        assert_eq!(
            request.delegated_input_sha256,
            request.effective_prompt_sha256()
        );
    }

    fn child_pre_admission_request() -> RegisterChildSessionRequest {
        let prompt = "exact delegated prompt";
        RegisterChildSessionRequest {
            parent_session_id: "commander-pre-admission".to_string(),
            parent_mission_revision_sha256: "a".repeat(64),
            commander_thread_id: Some("thread-pre-admission".to_string()),
            child_session_id: "child-pre-admission".to_string(),
            child_runtime_id: "runtime-pre-admission".to_string(),
            child_transaction_id: "transaction-pre-admission".to_string(),
            child_lease_id: "lease-pre-admission".to_string(),
            callback_request_id: "transaction-pre-admission".to_string(),
            effect_id: "runtime-pre-admission.message".to_string(),
            callback_delivery_route:
                router_contract::CallbackDeliveryRoute::TrustedTuraDirectThreadWriter,
            delegated_input_sha256: session_lifecycle::canonical_value_sha256(&json!(prompt)),
            session_directory: "/tmp/child-pre-admission".to_string(),
            session_name: "child pre-admission".to_string(),
            created_at_ms: 1,
            execution_payload: json!({"prompt": prompt}),
        }
    }

    #[test]
    fn direct_writer_route_is_durable_across_admission_response_and_replay_validation() {
        let request = child_pre_admission_request();
        let ready_set_identity = ChildReadySetIdentity::new(
            "mission-direct-writer",
            "task-direct-writer",
            "a".repeat(64),
            "b".repeat(64),
            "c".repeat(64),
            "d".repeat(64),
            "e".repeat(64),
            vec!["src/**".to_string()],
            vec![],
            vec![],
        );
        let admission = child_admission_record(&request, ready_set_identity);
        assert_eq!(
            admission.callback_delivery_route,
            Some(session_lifecycle::CallbackDeliveryRoute::TrustedTuraDirectThreadWriter)
        );
        validate_existing_child_admission(&admission, &request).expect("exact admission replay");

        let response = register_child_session_response(
            request.clone(),
            router_contract::RegisterChildSessionOutcome::Admitted,
        )
        .expect("child admission response");
        assert_eq!(
            response["callback_delivery_route"],
            "trusted_tura_direct_thread_writer"
        );

        let mut legacy = admission;
        legacy.callback_delivery_route = None;
        assert!(
            validate_existing_child_admission(&legacy, &request)
                .expect_err("legacy admission cannot replay")
                .to_string()
                .contains("CHILD_DIRECT_WRITER_LEGACY_ADMISSION_NOT_REPLAYABLE")
        );
    }

    fn blocked_child_ready_set_fixture(
        workspace: &std::path::Path,
    ) -> (TaskPlan, RegisterChildSessionRequest) {
        let prompt = "exact blocked dependency prompt";
        let delegated_input_sha256 = session_lifecycle::canonical_value_sha256(&json!(prompt));
        let exact_input_sha256 = "d".repeat(64);
        let mut jspace_contract = json!({
            "schema_version": "jspace_contract_v2",
            "repo_root": workspace,
            "dcf_generation": {
                "repo_root": workspace,
                "generation_id": "generation-blocked-ready-set",
                "required_domain_bindings": {
                    "surface-map": {
                        "required_domains": ["surface"],
                        "source_fingerprints": {"surface": "surface-blocked-ready-set"}
                    }
                }
            },
            "provenance": {"matched_surface_ids": ["surface"]},
            "matched_surface_ids": ["surface"],
            "read_scopes": ["src/**"],
            "write_scopes": [],
            "allowed_operations": ["read"],
            "denied_operations": ["create", "modify", "network", "install", "system_mutation"],
            "command_templates": [],
            "focused_verifiers": [],
            "declared_targets": [],
            "expansion": {
                "mode": "exact_target_only",
                "error_code": "JSPACE_EXPANSION_REQUIRED",
                "mutation_on_expansion": false
            }
        });
        let jspace_authorization_semantic_sha256 =
            tura_path::jspace::authorization_semantic_sha256(&jspace_contract)
                .expect("J-Space authorization digest");
        jspace_contract["authorization_semantic_sha256"] =
            json!(jspace_authorization_semantic_sha256);
        let jspace_content_sha256 = session_lifecycle::canonical_value_sha256(&jspace_contract);
        jspace_contract["content_sha256"] = json!(jspace_content_sha256);

        let mut task_context_capsule = json!({
            "schema_version": TASK_CONTEXT_CAPSULE_SCHEMA_VERSION,
            "mission": {
                "mission_id": "mission-blocked-ready-set",
                "task_id": "task-candidate",
                "mode": "DELIVERY",
                "current_predicate": "dependency.complete",
                "objective": "Dispatch the candidate only after its dependency is complete"
            },
            "context_summary": "The dependency remains pending, so child admission must have zero durable effects.",
            "dcf_generation": {"generation_id": "generation-blocked-ready-set"},
            "surface": {"repo_root": workspace, "matched_surface_ids": ["surface"]},
            "authority": {"forbidden_effects": ["live_runtime"]},
            "evidence_refs": [{
                "id": "exact-input",
                "kind": "artifact",
                "sha256": exact_input_sha256
            }],
            "focused_verifiers": [{"command": "cargo test -p router commander_task_packet_blocked_ready_set_has_zero_durable_effects"}],
            "jspace_semantic_sha256": jspace_authorization_semantic_sha256
        });
        let task_context_capsule_semantic_sha256 =
            session_lifecycle::canonical_value_sha256(&task_context_capsule);
        task_context_capsule["semantic_sha256"] = json!(task_context_capsule_semantic_sha256);

        let mut scheduling_contract = TaskSchedulingContractV1 {
            schema_version: lifecycle::TASK_SCHEDULING_CONTRACT_SCHEMA_VERSION.to_string(),
            mission_id: "mission-blocked-ready-set".to_string(),
            semantic_dispatch_key: "0".repeat(64),
            authority_mission_revision_sha256: "a".repeat(64),
            delegated_input_sha256: delegated_input_sha256.clone(),
            task_context_capsule_semantic_sha256,
            dependency_task_ids: vec!["task-dependency".to_string()],
            exact_input_sha256s: vec![exact_input_sha256],
            jspace_authorization_semantic_sha256,
            read_scopes: vec!["src/**".to_string()],
            write_scopes: vec![],
            declared_targets: vec![],
            conflict_identities: vec![],
            maximum_parallel_runtime_workers: 24,
        };
        scheduling_contract.semantic_dispatch_key =
            scheduling_contract.semantic_dispatch_sha256("task-candidate");
        let task_plan = TaskPlan {
            plan_summary: "blocked dependency plan".to_string(),
            detailed_tasks: vec![
                TaskStep {
                    task_id: "task-dependency".to_string(),
                    status: PlanStatus::Todo,
                    ..TaskStep::default()
                },
                TaskStep {
                    task_id: "task-candidate".to_string(),
                    start_condition: StartCondition::UserAction,
                    scheduling_contract: Some(scheduling_contract),
                    ..TaskStep::default()
                },
            ],
        };
        let request = RegisterChildSessionRequest {
            parent_session_id: "commander-blocked-ready-set".to_string(),
            parent_mission_revision_sha256: "a".repeat(64),
            commander_thread_id: Some("thread-blocked-ready-set".to_string()),
            child_session_id: "child-blocked-ready-set".to_string(),
            child_runtime_id: "runtime-blocked-ready-set".to_string(),
            child_transaction_id: "transaction-blocked-ready-set".to_string(),
            child_lease_id: "lease-blocked-ready-set".to_string(),
            callback_request_id: "transaction-blocked-ready-set".to_string(),
            effect_id: "runtime-blocked-ready-set.message".to_string(),
            callback_delivery_route:
                router_contract::CallbackDeliveryRoute::TrustedTuraDirectThreadWriter,
            delegated_input_sha256,
            session_directory: workspace.join("child").display().to_string(),
            session_name: "blocked ready-set child".to_string(),
            created_at_ms: 2,
            execution_payload: json!({
                "prompt": prompt,
                "task_context_capsule": task_context_capsule,
                "jspace_contract": jspace_contract
            }),
        };
        (task_plan, request)
    }

    fn commander_packet_from_child_request(
        request: &RegisterChildSessionRequest,
    ) -> CommanderTaskPacketV1 {
        CommanderTaskPacketV1 {
            schema_version: COMMANDER_TASK_PACKET_SCHEMA_VERSION.to_string(),
            parent_session_id: request.parent_session_id.clone(),
            parent_mission_revision_sha256: request.parent_mission_revision_sha256.clone(),
            commander_thread_id: request
                .commander_thread_id
                .clone()
                .expect("fixture commander thread"),
            task_id: "task-candidate".to_string(),
            session_directory: request.session_directory.clone(),
            session_name: request.session_name.clone(),
            created_at_ms: request.created_at_ms,
            prompt: request.execution_payload["prompt"]
                .as_str()
                .expect("fixture prompt")
                .to_string(),
            model: Some("official_codex_app_server/gpt-5.6-sol".to_string()),
            agent: Some("balanced".to_string()),
            session_type: Some("child".to_string()),
            service_tier: Some(runtime_contract::ModelServiceTier::Ultrafast),
            maximum_parallel_runtime_workers: 24,
            task_context_capsule: request.execution_payload["task_context_capsule"].clone(),
            jspace_contract: request.execution_payload["jspace_contract"].clone(),
            native_codex_execution: None,
        }
    }

    fn native_commander_packet_fixture(
        workspace: &std::path::Path,
    ) -> (TaskPlan, CommanderTaskPacketV1) {
        let (mut task_plan, request) = blocked_child_ready_set_fixture(workspace);
        let mut packet = commander_packet_from_child_request(&request);
        let mut delta_value = json!({
            "schema_version": NATIVE_CODEX_TASK_DELTA_SCHEMA_VERSION,
            "mission_id": "mission-blocked-ready-set",
            "mission_revision_sha256": packet.parent_mission_revision_sha256,
            "task_id": packet.task_id,
            "current_predicate": "dependency.complete",
            "instruction": packet.prompt,
        });
        delta_value["semantic_sha256"] =
            json!(session_lifecycle::canonical_value_sha256(&delta_value));
        let task_delta =
            NativeCodexTaskDelta::from_value(delta_value).expect("valid Native Codex task delta");
        let mut binding_value = json!({
            "schema_version": NATIVE_CODEX_EXECUTION_BINDING_SCHEMA_VERSION,
            "execution_profile_sha256": "1".repeat(64),
            "task_delta": task_delta,
            "codex_executable": "/Applications/ChatGPT.app/Contents/Resources/codex",
            "codex_executable_sha256": "2".repeat(64),
            "command_graph_executable": "/tmp/tura_command_graph",
            "command_graph_executable_sha256": "3".repeat(64),
            "command_graph_allowed_commands": ["zsh"],
            "sandbox": "read_only",
            "timeout_ms": 300000,
        });
        binding_value["semantic_sha256"] =
            json!(session_lifecycle::canonical_value_sha256(&binding_value));
        let binding = NativeCodexExecutionBinding::from_value(binding_value)
            .expect("valid Native Codex execution binding");

        packet.task_context_capsule["evidence_refs"]
            .as_array_mut()
            .expect("capsule evidence refs")
            .push(json!({
                "id": "native-codex-execution-binding",
                "kind": "execution_binding",
                "sha256": binding.semantic_sha256,
            }));
        packet
            .task_context_capsule
            .as_object_mut()
            .expect("capsule object")
            .remove("semantic_sha256");
        let capsule_sha256 =
            session_lifecycle::canonical_value_sha256(&packet.task_context_capsule);
        packet.task_context_capsule["semantic_sha256"] = json!(capsule_sha256);
        packet.native_codex_execution = Some(binding.clone());

        let contract = task_plan
            .detailed_tasks
            .iter_mut()
            .find(|task| task.task_id == packet.task_id)
            .and_then(|task| task.scheduling_contract.as_mut())
            .expect("candidate scheduling contract");
        contract.task_context_capsule_semantic_sha256 = capsule_sha256;
        contract.exact_input_sha256s = packet.task_context_capsule["evidence_refs"]
            .as_array()
            .expect("capsule evidence refs")
            .iter()
            .map(|reference| {
                reference["sha256"]
                    .as_str()
                    .expect("exact evidence digest")
                    .to_string()
            })
            .collect();
        contract.exact_input_sha256s.sort();
        contract.exact_input_sha256s.dedup();
        contract.semantic_dispatch_key = contract.semantic_dispatch_sha256(&packet.task_id);
        (task_plan, packet)
    }

    #[test]
    fn commander_native_codex_binding_compiles_to_validated_child_payload() {
        let root = tempfile::tempdir().expect("temp workspace");
        let workspace = root.path().join("workspace");
        std::fs::create_dir_all(workspace.join("child")).expect("create workspace");
        let (task_plan, packet) = native_commander_packet_fixture(&workspace);
        let compiled = compile_commander_task_packet_components(
            &packet,
            CommanderCompileParent {
                session_id: &packet.parent_session_id,
                workspace: workspace.to_str().expect("workspace path"),
                model: Some("official_codex_app_server/gpt-5.6-sol"),
                agent: Some("balanced"),
                session_type: "child",
                task_plan: &task_plan,
            },
        )
        .expect("compile Native Codex task packet");

        assert_eq!(
            compiled.request.execution_payload["task_id"],
            packet.task_id
        );
        assert_eq!(
            compiled.request.execution_payload["native_codex_execution"],
            serde_json::to_value(
                packet
                    .native_codex_execution
                    .as_ref()
                    .expect("packet binding"),
            )
            .expect("serialize binding")
        );
        validated_child_execution_payload(&compiled.request)
            .expect("compiled Native child payload validates before durable claim");

        let mut prompt_drift = compiled.request;
        prompt_drift.execution_payload["prompt"] = json!("different prompt");
        assert!(
            validated_child_execution_payload(&prompt_drift)
                .expect_err("prompt drift must fail before child admission")
                .to_string()
                .contains("NATIVE_CODEX_EXECUTION_BINDING_TASK_IDENTITY_MISMATCH")
        );
    }

    #[test]
    fn commander_task_packet_compiles_deterministically_without_mutation() {
        let root = tempfile::tempdir().expect("temp workspace");
        let workspace = root.path().join("workspace");
        std::fs::create_dir_all(workspace.join("child")).expect("create workspace");
        let (task_plan, request) = blocked_child_ready_set_fixture(&workspace);
        let packet = commander_packet_from_child_request(&request);
        let compile = || {
            compile_commander_task_packet_components(
                &packet,
                CommanderCompileParent {
                    session_id: &request.parent_session_id,
                    workspace: workspace.to_str().expect("workspace path"),
                    model: Some("official_codex_app_server/gpt-5.6-sol"),
                    agent: Some("balanced"),
                    session_type: "child",
                    task_plan: &task_plan,
                },
            )
            .expect("compile task packet")
        };

        let first = compile();
        let second = compile();
        assert_eq!(first.result, second.result);
        assert_eq!(first.request, second.request);
        assert_eq!(
            first.request.execution_payload["task_id"],
            first.result.task_id
        );
        assert_eq!(
            first.request.execution_payload["worker_env"]["TURA_PROJECT_ROOT"],
            workspace.display().to_string()
        );
        assert_eq!(
            first.request.execution_payload["worker_env"]
                [runtime_contract::SESSION_SERVICE_TIER_ENV],
            "ultrafast"
        );
        assert_eq!(
            first.result.mutation_counts,
            CommanderMutationCounts::zero()
        );
        assert_eq!(
            first.result.callback_delivery_route,
            WireCallbackDeliveryRoute::TrustedTuraDirectThreadWriter
        );
        assert_eq!(
            first.request.callback_request_id,
            first.request.child_transaction_id
        );
        assert_eq!(first.request.effect_id, first.request.canonical_effect_id());
    }

    #[test]
    fn commander_task_packet_replay_uses_durable_claim_preimage() {
        let root = tempfile::tempdir().expect("temp workspace");
        let workspace = root.path().join("workspace");
        std::fs::create_dir_all(workspace.join("child")).expect("create workspace");
        let (task_plan, request) = blocked_child_ready_set_fixture(&workspace);
        let packet = commander_packet_from_child_request(&request);
        let compile = |packet: &CommanderTaskPacketV1, task_plan: &TaskPlan| {
            compile_commander_task_packet_components(
                packet,
                CommanderCompileParent {
                    session_id: &request.parent_session_id,
                    workspace: workspace.to_str().expect("workspace path"),
                    model: Some("official_codex_app_server/gpt-5.6-sol"),
                    agent: Some("balanced"),
                    session_type: "child",
                    task_plan,
                },
            )
        };

        let before_claim = compile(&packet, &task_plan).expect("compile before claim");
        let mut claimed_plan = task_plan.clone();
        let claimed_task = claimed_plan
            .detailed_tasks
            .iter_mut()
            .find(|task| task.task_id == packet.task_id)
            .expect("claimed task");
        let contract = claimed_task
            .scheduling_contract
            .as_ref()
            .expect("scheduling contract");
        claimed_task.status = PlanStatus::Doing;
        claimed_task.sub_session_id = before_claim.result.child_session_id.clone();
        claimed_task.dispatch_claim = Some(lifecycle::TaskDispatchClaimV1 {
            schema_version: lifecycle::TASK_DISPATCH_CLAIM_SCHEMA_VERSION.to_string(),
            mission_id: contract.mission_id.clone(),
            task_id: packet.task_id.clone(),
            child_session_id: before_claim.result.child_session_id.clone(),
            child_runtime_id: before_claim.result.child_runtime_id.clone(),
            child_lease_id: before_claim.result.child_lease_id.clone(),
            child_transaction_id: before_claim.result.child_transaction_id.clone(),
            semantic_dispatch_key: before_claim.result.semantic_dispatch_key.clone(),
            authority_mission_revision_sha256: packet.parent_mission_revision_sha256.clone(),
            delegated_input_sha256: before_claim.result.delegated_input_sha256.clone(),
            task_context_capsule_semantic_sha256: before_claim
                .result
                .task_context_capsule_semantic_sha256
                .clone(),
            parent_task_plan_sha256: before_claim.result.parent_task_plan_sha256.clone(),
            task_scheduling_contract_sha256: before_claim
                .result
                .task_scheduling_contract_sha256
                .clone(),
            scope_claim_sha256: before_claim.result.scope_claim_sha256.clone(),
        });

        let after_claim = compile(&packet, &claimed_plan).expect("compile after exact claim");
        assert_eq!(after_claim.result, before_claim.result);
        assert_eq!(after_claim.request, before_claim.request);

        let mut changed_packet = packet.clone();
        changed_packet.session_name.push_str(" changed");
        let changed = compile(&changed_packet, &claimed_plan).expect("compile changed packet");
        assert_ne!(
            changed.result.compile_identity_sha256,
            before_claim.result.compile_identity_sha256
        );
        assert_ne!(
            changed.result.child_session_id,
            before_claim.result.child_session_id
        );

        let mut conflicting_plan = claimed_plan;
        conflicting_plan
            .detailed_tasks
            .iter_mut()
            .find(|task| task.task_id == packet.task_id)
            .and_then(|task| task.dispatch_claim.as_mut())
            .expect("conflicting claim")
            .scope_claim_sha256 = "f".repeat(64);
        let conflict = match compile(&packet, &conflicting_plan) {
            Ok(_) => panic!("conflicting claim must fail closed"),
            Err(error) => error,
        };
        assert_eq!(conflict.to_string(), "TASK_PACKET_DURABLE_CLAIM_MISMATCH");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn commander_task_packet_exact_replay_has_one_durable_admission() {
        use std::os::unix::fs::PermissionsExt;

        let _lock = crate::services::ROUTER_TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let root = tempfile::tempdir().expect("isolated replay identity root");
        let mock_runtime = root.path().join("tura_runtime");
        std::fs::write(
            &mock_runtime,
            "#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '{\"ok\":true,\"result\":{\"output\":\"mock\"}}'\n",
        )
        .expect("mock one-shot runtime");
        let mut permissions = std::fs::metadata(&mock_runtime)
            .expect("mock runtime metadata")
            .permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(&mock_runtime, permissions).expect("mock runtime executable");

        let project = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(std::path::Path::parent)
            .expect("project root");
        let workspace = root.path().join("workspace");
        std::fs::create_dir_all(workspace.join("child")).expect("workspace");
        let state = build_state();
        let _env = RecoveryTestEnvGuard::install(state.session_db.clone(), root.path(), project);
        let _session_db = InProcessSessionDbGuard::start();

        let (mut task_plan, request) = blocked_child_ready_set_fixture(&workspace);
        let direct_task = task_plan
            .detailed_tasks
            .iter()
            .find(|task| task.task_id == "task-candidate")
            .expect("direct-dispatch task");
        assert_eq!(direct_task.start_condition, StartCondition::UserAction);
        assert!(!direct_task.scheduler_eligible(chrono::Utc::now()));
        task_plan
            .detailed_tasks
            .iter_mut()
            .find(|task| task.task_id == "task-dependency")
            .expect("dependency task")
            .status = PlanStatus::Done;
        let created = session_log_contract::client::call_service(
            &session_log_contract::SessionLogCommand::CreateSession(Box::new(
                session_log_contract::CreateSessionRequest {
                    command_id: "create-replay-identity-parent".into(),
                    session_id: request.parent_session_id.clone(),
                    creation_command: lifecycle::SessionCommand::CreateSession {
                        task_plan: task_plan.clone(),
                    },
                    copy_context: false,
                    workspace: workspace.display().to_string(),
                    session_directory: workspace.display().to_string(),
                    name: "replay identity parent".into(),
                    created_at: 1,
                    model: None,
                    agent: None,
                    session_type: "coding".into(),
                    kill_processes_on_start: false,
                    validator_enabled: false,
                    force_planning: false,
                    model_variant: None,
                    model_acceleration_enabled: false,
                    disable_permission_restrictions: true,
                    use_last_tool_call_response: false,
                    auto_session_name: false,
                    initial_task_plan_patch: None,
                },
            )),
        )
        .expect("create replay identity parent");
        assert!(matches!(
            created,
            session_log_contract::SessionLogResponse::SessionCommandApplied { .. }
        ));

        let service = ExecutionService::new();
        let packet = commander_packet_from_child_request(&request);
        let packet_value = serde_json::to_value(&packet).expect("packet value");
        let stale_prepared = service
            .prepare_commander_task_packet_dispatch(&state, packet_value.clone())
            .await
            .expect("prepare before parent plan drift");
        let stale_compilation = stale_prepared.result.clone();

        let mut drifted_task_plan = task_plan;
        drifted_task_plan
            .plan_summary
            .push_str(" unrelated parent plan update");
        let drifted = session_log_contract::client::call_service(
            &session_log_contract::SessionLogCommand::ExecuteSessionCommand(
                session_log_contract::ExecuteSessionCommandRequest {
                    command_id: "drift-replay-identity-parent-plan".into(),
                    session_id: request.parent_session_id.clone(),
                    session_command: lifecycle::SessionCommand::ApplyTaskStatus {
                        task_plan: drifted_task_plan,
                    },
                    message_projection: None,
                },
            ),
        )
        .expect("apply unrelated parent plan drift");
        assert!(matches!(
            drifted,
            session_log_contract::SessionLogResponse::SessionCommandApplied { .. }
        ));

        let store = lifecycle_store(&packet.parent_session_id).expect("lifecycle store");
        let parent_after_drift = read_session_snapshot(&packet.parent_session_id)
            .expect("parent after drift")
            .expect("parent exists after drift");
        let lifecycle_after_drift = store.readback().expect("lifecycle after drift");
        let stale_error = service
            .dispatch_prepared_commander_task_packet(&state, stale_prepared)
            .await
            .expect_err("stale prepared packet must fail closed");
        assert!(
            stale_error
                .to_string()
                .contains("TASK_PACKET_PRE_ADMISSION:TASK_PACKET_PREPARED_PARENT_TASK_PLAN_DRIFT")
        );
        assert_eq!(
            read_session_snapshot(&packet.parent_session_id)
                .expect("parent after stale rejection")
                .expect("parent remains after stale rejection"),
            parent_after_drift
        );
        assert_eq!(
            store.readback().expect("lifecycle after stale rejection"),
            lifecycle_after_drift
        );
        assert!(
            read_session_snapshot(&stale_compilation.child_session_id)
                .expect("stale child lookup")
                .is_none()
        );
        assert!(
            store
                .child_admission(&stale_compilation.child_session_id)
                .expect("stale child admission lookup")
                .is_none()
        );
        assert!(service.sessions.lock().is_empty());
        assert_eq!(service.runtime_slots.active_count(), 0);
        assert_eq!(state.manager.count_workers_with_prefix(""), 0);

        let prepared = service
            .prepare_commander_task_packet_dispatch(&state, packet_value.clone())
            .await
            .expect("prepare after parent plan drift");
        let before_claim = prepared.result.clone();
        assert_ne!(
            before_claim.parent_task_plan_sha256,
            stale_compilation.parent_task_plan_sha256
        );
        assert_ne!(
            before_claim.compile_identity_sha256,
            stale_compilation.compile_identity_sha256
        );
        assert_ne!(
            before_claim.child_session_id,
            stale_compilation.child_session_id
        );
        let first: CommanderTaskPacketDispatchResponse = serde_json::from_value(
            service
                .dispatch_prepared_commander_task_packet(&state, prepared)
                .await
                .expect("first dispatch"),
        )
        .expect("first dispatch result");
        assert_eq!(first.compilation, before_claim);
        assert_eq!(
            first.admission.outcome,
            RegisterChildSessionOutcome::Admitted
        );
        assert_eq!(first.duplicate_effect_count, 0);

        let after_claim: CommanderTaskPacketCompileResult = serde_json::from_value(
            service
                .compile_commander_task_packet_request(&state, packet_value.clone())
                .await
                .expect("compile after claim"),
        )
        .expect("compile result after claim");
        assert_eq!(after_claim, before_claim);

        let parent_after_first = read_session_snapshot(&packet.parent_session_id)
            .expect("parent after first")
            .expect("parent exists after first");
        let child_after_first = read_session_snapshot(&before_claim.child_session_id)
            .expect("child after first")
            .expect("child exists after first");
        let runtime_after_first = service
            .get_runtime_lease(
                &state,
                serde_json::to_value(session_log_contract::GetRuntimeLeaseRequest {
                    runtime_id: before_claim.child_runtime_id.clone(),
                    database_path: None,
                })
                .expect("runtime request"),
            )
            .await
            .expect("runtime after first");
        let admission_after_first = store
            .child_admission(&before_claim.child_session_id)
            .expect("admission after first")
            .expect("durable admission after first");
        let lifecycle_after_first = store.readback().expect("lifecycle after first");
        assert_eq!(
            parent_after_first
                .lifecycle_projection
                .task_plan
                .detailed_tasks
                .iter()
                .filter(|task| task.dispatch_claim.is_some())
                .count(),
            1
        );

        let second: CommanderTaskPacketDispatchResponse = serde_json::from_value(
            service
                .dispatch_commander_task_packet_request(&state, packet_value)
                .await
                .expect("exact replay dispatch"),
        )
        .expect("exact replay result");
        assert_eq!(second.compilation, before_claim);
        assert_eq!(
            second.admission.outcome,
            RegisterChildSessionOutcome::AlreadyAdmitted
        );
        assert_eq!(second.duplicate_effect_count, 0);
        assert_eq!(
            read_session_snapshot(&packet.parent_session_id)
                .expect("parent after replay")
                .expect("parent remains after replay"),
            parent_after_first
        );
        assert_eq!(
            read_session_snapshot(&before_claim.child_session_id)
                .expect("child after replay")
                .expect("child remains after replay"),
            child_after_first
        );
        assert_eq!(
            service
                .get_runtime_lease(
                    &state,
                    serde_json::to_value(session_log_contract::GetRuntimeLeaseRequest {
                        runtime_id: before_claim.child_runtime_id.clone(),
                        database_path: None,
                    })
                    .expect("replay runtime request"),
                )
                .await
                .expect("runtime after replay"),
            runtime_after_first
        );
        assert_eq!(
            store
                .child_admission(&before_claim.child_session_id)
                .expect("admission after replay")
                .expect("durable admission after replay"),
            admission_after_first
        );
        assert_eq!(
            store.readback().expect("lifecycle after replay"),
            lifecycle_after_first
        );

        let mut changed_packet = packet;
        changed_packet.session_name.push_str(" changed");
        let changed_value = serde_json::to_value(&changed_packet).expect("changed packet");
        let changed: CommanderTaskPacketCompileResult = serde_json::from_value(
            service
                .compile_commander_task_packet_request(&state, changed_value.clone())
                .await
                .expect("compile changed packet"),
        )
        .expect("changed compile result");
        assert_ne!(
            changed.compile_identity_sha256,
            before_claim.compile_identity_sha256
        );
        let changed_error = service
            .dispatch_commander_task_packet_request(&state, changed_value)
            .await
            .expect_err("changed packet must fail against durable claim");
        assert!(
            changed_error
                .to_string()
                .contains("CHILD_READY_SET_DURABLE_CLAIM_REQUEST_MISMATCH")
        );
        assert_eq!(
            store.readback().expect("lifecycle after changed packet"),
            lifecycle_after_first
        );
        assert!(service.sessions.lock().is_empty());
        assert_eq!(service.runtime_slots.active_count(), 0);
        assert_eq!(state.manager.count_workers_with_prefix(""), 0);
    }

    #[test]
    fn commander_task_packet_mismatches_fail_before_compiled_child_identity() {
        let root = tempfile::tempdir().expect("temp workspace");
        let workspace = root.path().join("workspace");
        std::fs::create_dir_all(workspace.join("child")).expect("create workspace");
        let (task_plan, request) = blocked_child_ready_set_fixture(&workspace);
        let packet = commander_packet_from_child_request(&request);
        let compile =
            |packet: &CommanderTaskPacketV1, task_plan: &TaskPlan, model: Option<&str>| {
                compile_commander_task_packet_components(
                    packet,
                    CommanderCompileParent {
                        session_id: &request.parent_session_id,
                        workspace: workspace.to_str().expect("workspace path"),
                        model,
                        agent: Some("balanced"),
                        session_type: "child",
                        task_plan,
                    },
                )
                .map(|compiled| compiled.result)
            };

        let mut changed = packet.clone();
        changed.schema_version = "tura_commander_task_packet_v0".to_string();
        assert!(
            compile(&changed, &task_plan, None)
                .unwrap_err()
                .to_string()
                .contains("SCHEMA")
        );

        changed = packet.clone();
        changed.parent_mission_revision_sha256 = "f".repeat(64);
        assert!(
            compile(&changed, &task_plan, None)
                .unwrap_err()
                .to_string()
                .contains("MISSION_REVISION_MISMATCH")
        );

        changed = packet.clone();
        changed.maximum_parallel_runtime_workers = 23;
        assert!(
            compile(&changed, &task_plan, None)
                .unwrap_err()
                .to_string()
                .contains("PARALLEL_LIMIT_MISMATCH")
        );

        changed = packet.clone();
        changed.jspace_contract["read_scopes"] = json!(["changed/**"]);
        assert!(compile(&changed, &task_plan, None).is_err());

        assert!(
            compile(&packet, &task_plan, Some("different-provider/model"))
                .unwrap_err()
                .to_string()
                .contains("MODEL_IDENTITY_MISMATCH")
        );

        let mut evidence_plan = task_plan.clone();
        let contract = evidence_plan.detailed_tasks[1]
            .scheduling_contract
            .as_mut()
            .expect("scheduling contract");
        contract.exact_input_sha256s = vec!["f".repeat(64)];
        contract.semantic_dispatch_key = contract.semantic_dispatch_sha256(&packet.task_id);
        assert!(
            compile(&packet, &evidence_plan, None)
                .unwrap_err()
                .to_string()
                .contains("EVIDENCE_DIGEST_MISMATCH")
        );
    }

    #[tokio::test]
    async fn commander_task_packet_blocked_ready_set_has_zero_durable_effects() {
        let _lock = crate::services::ROUTER_TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let root = tempfile::tempdir().expect("isolated blocked ready-set root");
        let project = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(std::path::Path::parent)
            .expect("project root");
        let workspace = root.path().join("workspace");
        std::fs::create_dir_all(workspace.join("src")).expect("workspace");
        let state = build_state();
        let _env = RecoveryTestEnvGuard::install(state.session_db.clone(), root.path(), project);
        let _session_db = InProcessSessionDbGuard::start();
        let (task_plan, request) = blocked_child_ready_set_fixture(&workspace);
        let direct_task = task_plan
            .detailed_tasks
            .iter()
            .find(|task| task.task_id == "task-candidate")
            .expect("direct-dispatch task");
        assert_eq!(direct_task.start_condition, StartCondition::UserAction);
        assert!(!direct_task.scheduler_eligible(chrono::Utc::now()));
        let created = session_log_contract::client::call_service(
            &session_log_contract::SessionLogCommand::CreateSession(Box::new(
                session_log_contract::CreateSessionRequest {
                    command_id: "create-blocked-ready-set-parent".into(),
                    session_id: request.parent_session_id.clone(),
                    creation_command: lifecycle::SessionCommand::CreateSession {
                        task_plan: task_plan.clone(),
                    },
                    copy_context: false,
                    workspace: workspace.display().to_string(),
                    session_directory: workspace.display().to_string(),
                    name: "blocked ready-set parent".into(),
                    created_at: 1,
                    model: None,
                    agent: None,
                    session_type: "coding".into(),
                    kill_processes_on_start: false,
                    validator_enabled: false,
                    force_planning: false,
                    model_variant: None,
                    model_acceleration_enabled: false,
                    disable_permission_restrictions: true,
                    use_last_tool_call_response: false,
                    auto_session_name: false,
                    initial_task_plan_patch: None,
                },
            )),
        )
        .expect("create parent session");
        assert!(matches!(
            created,
            session_log_contract::SessionLogResponse::SessionCommandApplied { .. }
        ));

        let service = ExecutionService::new();
        let parent_before = read_session_snapshot(&request.parent_session_id)
            .expect("parent before")
            .expect("parent exists");
        let store = lifecycle_store(&request.parent_session_id).expect("lifecycle store");
        assert!(
            store
                .child_admission(&request.child_session_id)
                .expect("admission before")
                .is_none()
        );

        let error = service
            .register_child_session_request(
                &state,
                serde_json::to_value(&request).expect("register child request"),
            )
            .await
            .expect_err("blocked dependency must stop child admission");
        assert_eq!(
            error.to_string(),
            "CHILD_READY_SET_BLOCKED:BlockedDependency:TASK_READY_SET_DEPENDENCY_NOT_TERMINAL"
        );

        let packet = commander_packet_from_child_request(&request);
        let project_agent_id = "workspace-only-commander";
        let project_agent_config =
            tura_agents::store::default_agent_config(&workspace, project_agent_id)
                .expect("workspace agent config");
        tura_agents::store::save_dynamic_agent(&workspace, &project_agent_config, None)
            .expect("save workspace-only agent");
        let mut project_agent_packet = packet.clone();
        project_agent_packet.agent = Some(project_agent_id.to_string());
        let project_agent_compiled = compile_commander_task_packet_components(
            &project_agent_packet,
            CommanderCompileParent {
                session_id: &request.parent_session_id,
                workspace: workspace.to_str().expect("workspace path"),
                model: None,
                agent: None,
                session_type: "coding",
                task_plan: &task_plan,
            },
        )
        .expect("compile workspace-only agent packet");
        let project_agent_payload =
            validated_child_execution_payload(&project_agent_compiled.request)
                .expect("workspace-only agent execution payload");
        let project_agent_enqueue = EnqueueTurnRequest {
            runtime_id: project_agent_compiled.request.child_runtime_id.clone(),
            session_id: project_agent_compiled.request.child_session_id.clone(),
            payload: project_agent_payload,
        };
        let project_agent_run = payload_to_run_agent_request(
            &project_agent_enqueue,
            &project_agent_compiled.request.child_lease_id,
            None,
        )
        .expect("workspace-only runtime request");
        let runtime_project_root =
            crate::runtime_dispatch::request_project_root(&project_agent_run)
                .expect("runtime project-root parsing")
                .expect("compiled project root");
        assert_eq!(runtime_project_root, workspace);
        let resolved_project_agent = state
            .registry
            .agents
            .resolve_for_project(
                project_agent_run.agent.as_deref(),
                project_agent_run.session_type.as_deref(),
                Some(&runtime_project_root),
            )
            .expect("runtime-equivalent workspace-only agent resolution");
        assert_eq!(resolved_project_agent.agent_name, project_agent_id);

        let mut named_route = packet.clone();
        named_route.model = Some("fast".to_string());
        let mut unknown_route = packet.clone();
        unknown_route.model = Some("missing-route".to_string());
        let mut malformed_explicit_model = packet.clone();
        malformed_explicit_model.model = Some("official_codex_app_server/".to_string());
        let mut unknown_agent = packet.clone();
        unknown_agent.agent = Some("missing-commander-agent".to_string());
        for (candidate, expected) in [
            (
                named_route,
                "TASK_PACKET_PRE_ADMISSION:TASK_PACKET_MODEL_AUTHORITY_REJECTED:invalid session model override `fast`: expected provider/model",
            ),
            (
                unknown_route,
                "TASK_PACKET_PRE_ADMISSION:TASK_PACKET_MODEL_AUTHORITY_REJECTED:invalid session model override `missing-route`: expected provider/model",
            ),
            (
                malformed_explicit_model,
                "TASK_PACKET_PRE_ADMISSION:TASK_PACKET_MODEL_AUTHORITY_REJECTED:invalid explicit model selection `official_codex_app_server/`: expected provider/model",
            ),
            (
                unknown_agent,
                "TASK_PACKET_PRE_ADMISSION:TASK_PACKET_AGENT_NOT_FOUND:missing-commander-agent",
            ),
        ] {
            let error = service
                .dispatch_commander_task_packet_request(
                    &state,
                    serde_json::to_value(candidate).expect("invalid authority packet"),
                )
                .await
                .expect_err("invalid provider or agent must stop before claim");
            assert_eq!(error.to_string(), expected);
            let unchanged = read_session_snapshot(&request.parent_session_id)
                .expect("parent readback after authority rejection")
                .expect("parent remains after authority rejection");
            assert_eq!(unchanged, parent_before);
            assert!(
                unchanged
                    .lifecycle_projection
                    .task_plan
                    .detailed_tasks
                    .iter()
                    .all(|task| task.dispatch_claim.is_none())
            );
            assert!(service.sessions.lock().is_empty());
            assert_eq!(service.runtime_slots.active_count(), 0);
            assert_eq!(state.manager.count_workers_with_prefix(""), 0);
        }

        for agent_id in [
            "coding_agent",
            "coding",
            "general_agent",
            "general",
            "balanced",
            project_agent_id,
        ] {
            let mut candidate = packet.clone();
            candidate.agent = Some(agent_id.to_string());
            let error = service
                .dispatch_commander_task_packet_request(
                    &state,
                    serde_json::to_value(candidate).expect("authoritative agent packet"),
                )
                .await
                .expect_err("resolved agent reaches the blocked ready-set");
            assert_eq!(
                error.to_string(),
                "TASK_PACKET_PRE_ADMISSION:CHILD_READY_SET_BLOCKED:BlockedDependency:TASK_READY_SET_DEPENDENCY_NOT_TERMINAL",
                "agent {agent_id} must resolve before ready-set validation"
            );
            let unchanged = read_session_snapshot(&request.parent_session_id)
                .expect("parent readback after resolved agent")
                .expect("parent remains after resolved agent");
            assert_eq!(unchanged, parent_before);
        }

        let compiled = compile_commander_task_packet_components(
            &packet,
            CommanderCompileParent {
                session_id: &request.parent_session_id,
                workspace: workspace.to_str().expect("workspace path"),
                model: None,
                agent: None,
                session_type: "coding",
                task_plan: &task_plan,
            },
        )
        .expect("compile blocked packet");
        let task_packet_error = service
            .dispatch_commander_task_packet_request(
                &state,
                serde_json::to_value(&packet).expect("task packet"),
            )
            .await
            .expect_err("blocked ready-set remains pre-admission");
        assert_eq!(
            task_packet_error.to_string(),
            "TASK_PACKET_PRE_ADMISSION:CHILD_READY_SET_BLOCKED:BlockedDependency:TASK_READY_SET_DEPENDENCY_NOT_TERMINAL"
        );

        let parent_after = read_session_snapshot(&request.parent_session_id)
            .expect("parent after")
            .expect("parent remains");
        assert_eq!(parent_after.lifecycle_projection.task_plan, task_plan);
        assert_eq!(parent_after, parent_before);
        assert!(
            parent_after
                .lifecycle_projection
                .task_plan
                .detailed_tasks
                .iter()
                .all(|task| task.dispatch_claim.is_none())
        );
        assert!(
            read_session_snapshot(&request.child_session_id)
                .expect("child readback")
                .is_none()
        );
        assert!(
            read_session_snapshot(&compiled.request.child_session_id)
                .expect("compiled child readback")
                .is_none()
        );
        assert!(
            store
                .child_admission(&request.child_session_id)
                .expect("admission after")
                .is_none()
        );
        assert!(
            store
                .child_admission(&compiled.request.child_session_id)
                .expect("compiled admission after")
                .is_none()
        );
        match session_log_contract::client::call_service(
            &session_log_contract::SessionLogCommand::GetRuntimeLease(
                session_log_contract::GetRuntimeLeaseRequest {
                    runtime_id: request.child_runtime_id.clone(),
                    database_path: None,
                },
            ),
        )
        .expect("runtime lease readback")
        {
            session_log_contract::SessionLogResponse::RuntimeLeaseRead { runtime: None } => {}
            other => panic!("unexpected runtime lease after blocked ready-set: {other:?}"),
        }
        assert!(service.sessions.lock().is_empty());
        assert_eq!(service.runtime_slots.active_count(), 0);
        assert_eq!(state.manager.count_workers_with_prefix(""), 0);
    }

    #[test]
    fn child_execution_contract_is_validated_before_durable_admission() {
        let valid = child_pre_admission_request();
        let payload = validated_child_execution_payload(&valid).expect("valid exact request");
        assert_eq!(payload["parent_session_id"], valid.parent_session_id);
        assert_eq!(
            payload["delegated_input_sha256"],
            valid.delegated_input_sha256
        );

        let mut malformed = Vec::new();

        let mut invalid_execution = valid.clone();
        invalid_execution.execution_payload = json!({"worker_env": "invalid"});
        malformed.push(invalid_execution);

        let mut invalid_digest = valid.clone();
        invalid_digest.delegated_input_sha256 = "b".repeat(64);
        malformed.push(invalid_digest);

        let mut invalid_capsule = valid.clone();
        invalid_capsule.execution_payload = json!({
            "prompt": "exact delegated prompt",
            "task_context_capsule": {"schema_version": "invalid"},
            "jspace_contract": {"semantic_sha256": "c".repeat(64)}
        });
        malformed.push(invalid_capsule);

        let mut missing_jspace = valid.clone();
        missing_jspace.execution_payload = json!({
            "prompt": "exact delegated prompt",
            "task_context_capsule": {"schema_version": "invalid"}
        });
        malformed.push(missing_jspace);

        let mut conflicting_context = valid.clone();
        conflicting_context.execution_payload = json!({
            "prompt": "exact delegated prompt",
            "runtime_context": "raw",
            "task_context_capsule": {"schema_version": "invalid"}
        });
        malformed.push(conflicting_context);

        for request in malformed {
            validated_child_execution_payload(&request)
                .expect_err("malformed request must fail before durable admission");
        }
    }

    #[test]
    fn task_packet_forwarder_disposition_reads_exact_durable_child_admission() {
        let root = tempfile::tempdir().expect("durable admission disposition root");
        let request = child_pre_admission_request();
        let store = SessionLifecycleStore::open(
            root.path(),
            &request.parent_session_id,
            LifecycleConfig::default(),
        )
        .expect("durable admission store");
        assert_eq!(
            durable_child_admission_disposition_from_store(&store, &request)
                .expect("pre-admission readback"),
            DurableChildAdmissionDisposition::NotAdmitted
        );

        let ready_set_identity = ChildReadySetIdentity::new(
            "mission-durable-admission",
            "task-durable-admission",
            "a".repeat(64),
            "b".repeat(64),
            "c".repeat(64),
            "d".repeat(64),
            "e".repeat(64),
            vec!["src/**".to_string()],
            Vec::new(),
            Vec::new(),
        );
        let admission = child_admission_record(&request, ready_set_identity);
        assert_eq!(
            store
                .admit_child(&admission)
                .expect("first durable admission"),
            ChildAdmissionOutcome::Admitted
        );
        assert_eq!(
            durable_child_admission_disposition_from_store(&store, &request)
                .expect("post-admission readback"),
            DurableChildAdmissionDisposition::Admitted
        );
        assert_eq!(
            store
                .admit_child(&admission)
                .expect("durable admission replay"),
            ChildAdmissionOutcome::AlreadyAdmitted
        );
        assert_eq!(
            durable_child_admission_disposition_from_store(&store, &request)
                .expect("replay readback"),
            DurableChildAdmissionDisposition::Admitted,
            "replay must retain the same forwarder without creating a duplicate admission"
        );

        let mut conflicting = request;
        conflicting.child_transaction_id = "transaction-conflict".to_string();
        assert_eq!(
            durable_child_admission_disposition_from_store(&store, &conflicting)
                .expect_err("mismatched durable admission must fail closed")
                .to_string(),
            format!(
                "CHILD_ADMISSION_IDENTITY_CONFLICT:{}",
                conflicting.child_session_id
            )
        );
    }

    #[test]
    fn child_completion_state_distinguishes_simple_and_task_managed_sessions() {
        assert_eq!(
            child_completion_state(SessionState::Running, &TaskPlan::default()),
            DurableChildCompletionState::EmptyPlanPending
        );
        assert_eq!(
            child_completion_state(SessionState::Completed, &TaskPlan::default()),
            DurableChildCompletionState::SimpleEmptyPlanTerminal
        );

        let plan = |statuses: &[PlanStatus]| TaskPlan {
            plan_summary: "delegated task".to_string(),
            detailed_tasks: statuses
                .iter()
                .enumerate()
                .map(|(index, status)| TaskStep {
                    task_id: format!("task-{index}"),
                    status: *status,
                    ..TaskStep::default()
                })
                .collect(),
        };
        assert_eq!(
            child_completion_state(SessionState::Running, &plan(&[PlanStatus::Doing])),
            DurableChildCompletionState::TaskManagedPending
        );
        assert_eq!(
            child_completion_state(
                SessionState::Completed,
                &plan(&[PlanStatus::Done, PlanStatus::Question]),
            ),
            DurableChildCompletionState::TaskManagedPending
        );
        assert_eq!(
            child_completion_state(
                SessionState::Running,
                &plan(&[PlanStatus::Done, PlanStatus::Archived]),
            ),
            DurableChildCompletionState::TaskManagedTerminal
        );
    }

    #[test]
    fn admitted_pre_execution_failure_is_durable_proven_zero_and_replayable_once() {
        let root = tempfile::tempdir().expect("pre-execution failure root");
        let request = child_pre_admission_request();
        let task_id = "task-pre-execution";
        let store = SessionLifecycleStore::open(
            root.path(),
            &request.parent_session_id,
            LifecycleConfig::default(),
        )
        .expect("pre-execution lifecycle store");
        let admission = child_admission_record(
            &request,
            ChildReadySetIdentity::new(
                "mission-pre-execution",
                task_id,
                "a".repeat(64),
                "b".repeat(64),
                "c".repeat(64),
                "d".repeat(64),
                "e".repeat(64),
                vec!["src/**".to_string()],
                Vec::new(),
                Vec::new(),
            ),
        );
        store
            .admit_child(&admission)
            .expect("durable pre-execution admission");
        let failure = anyhow::anyhow!("fixture enqueue failed before runtime registration");
        let (transport, delivery) = publish_admitted_pre_execution_failure_callback_from_store(
            &store,
            &request,
            task_id,
            Some(&failure),
        )
        .expect("publish typed pre-execution failure");

        assert_eq!(transport["method"], "session.terminal_failure");
        assert_eq!(
            transport["payload"]["body"]["item"]["classification"],
            "PROVEN_ZERO_EFFECT"
        );
        assert!(matches!(
            delivery.callback_effect_identity,
            Some(CallbackEffectIdentity::ProvenZeroEffect { .. })
        ));
        let receipt = admitted_pre_execution_failure_receipt_from_store(&store, &request, task_id)
            .expect("pre-execution receipt readback")
            .expect("pre-execution receipt");
        assert_eq!(receipt.terminal_state, TerminalState::Interrupted);
        assert!(receipt.history_readable);
        assert!(receipt.follow_up_capable);

        let replay = replay_terminal_callbacks_from_store(
            &store,
            &request.parent_session_id,
            &request.child_session_id,
            &request.child_transaction_id,
        )
        .expect("exact pre-execution callback replay");
        assert_eq!(replay.len(), 1);
        assert_eq!(replay[0].0, transport);
        assert_eq!(replay[0].1, delivery);

        let duplicate = publish_admitted_pre_execution_failure_callback_from_store(
            &store, &request, task_id, None,
        )
        .expect("idempotent pre-execution failure publication");
        assert_eq!(duplicate, (transport, delivery));
    }

    #[tokio::test]
    async fn pre_execution_terminalization_failure_publishes_zero_callbacks() {
        let _lock = crate::services::ROUTER_TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let root = tempfile::tempdir().expect("pre-execution terminalization root");
        let project = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(std::path::Path::parent)
            .expect("project root");
        let workspace = root.path().join("workspace");
        let child_directory = workspace.join("child");
        std::fs::create_dir_all(&child_directory).expect("child workspace");
        let state = build_state();
        let _env = RecoveryTestEnvGuard::install(state.session_db.clone(), root.path(), project);
        state.session_db.start().expect("isolated session db");

        let mut request = child_pre_admission_request();
        request.session_directory = child_directory.display().to_string();
        let created = session_log_contract::client::call_service(
            &session_log_contract::SessionLogCommand::CreateSession(Box::new(
                session_log_contract::CreateSessionRequest {
                    command_id: "create-pre-execution-parent".into(),
                    session_id: request.parent_session_id.clone(),
                    creation_command: lifecycle::SessionCommand::CreateSession {
                        task_plan: lifecycle::TaskPlan::default(),
                    },
                    copy_context: false,
                    workspace: workspace.display().to_string(),
                    session_directory: workspace.display().to_string(),
                    name: "pre-execution parent".into(),
                    created_at: 1,
                    model: None,
                    agent: None,
                    session_type: "coding".into(),
                    kill_processes_on_start: false,
                    validator_enabled: false,
                    force_planning: false,
                    model_variant: None,
                    model_acceleration_enabled: false,
                    disable_permission_restrictions: true,
                    use_last_tool_call_response: false,
                    auto_session_name: false,
                    initial_task_plan_patch: None,
                },
            )),
        )
        .expect("create parent session");
        assert!(matches!(
            created,
            session_log_contract::SessionLogResponse::SessionCommandApplied { .. }
        ));
        let parent = read_session_snapshot(&request.parent_session_id)
            .expect("parent readback")
            .expect("parent session");
        create_child_session(&parent, &request).expect("create admitted child session");

        let paused = session_log_contract::client::call_service(
            &session_log_contract::SessionLogCommand::ExecuteSessionCommand(
                session_log_contract::ExecuteSessionCommandRequest {
                    command_id: "pause-pre-execution-child".into(),
                    session_id: request.child_session_id.clone(),
                    session_command: lifecycle::SessionCommand::ApplyRuntimeState {
                        state: SessionState::Paused,
                    },
                    message_projection: None,
                },
            ),
        )
        .expect("pause child session");
        assert!(matches!(
            paused,
            session_log_contract::SessionLogResponse::SessionCommandApplied { result }
                if result.projection.state == SessionState::Paused
        ));

        let task_id = "task-pre-execution-terminalization";
        let store = lifecycle_store(&request.parent_session_id).expect("lifecycle store");
        let admission = child_admission_record(
            &request,
            ChildReadySetIdentity::new(
                "mission-pre-execution-terminalization",
                task_id,
                "a".repeat(64),
                "b".repeat(64),
                "c".repeat(64),
                "d".repeat(64),
                "e".repeat(64),
                vec!["src/**".to_string()],
                Vec::new(),
                Vec::new(),
            ),
        );
        store
            .admit_child(&admission)
            .expect("durable child admission");

        let error = ExecutionService::new()
            .terminalize_admitted_child_pre_execution_failure(
                &state,
                &request,
                task_id,
                &anyhow::anyhow!("fixture enqueue failed"),
            )
            .await
            .expect_err("paused child cannot be terminalized as unstarted");
        assert_eq!(
            error.to_string(),
            format!(
                "ADMITTED_PRE_EXECUTION_FAILURE_CHILD_SESSION_NOT_UNSTARTED:{}:Paused",
                request.child_session_id
            )
        );
        assert!(
            admitted_pre_execution_failure_receipt_from_store(&store, &request, task_id)
                .expect("terminal receipt readback")
                .is_none()
        );
        assert!(
            store
                .callbacks_for_replay()
                .expect("callback replay readback")
                .is_empty()
        );
        assert_eq!(
            store
                .readback()
                .expect("lifecycle readback")
                .intaken_callbacks,
            0
        );

        let mut successful = request.clone();
        successful.child_session_id = "child-pre-execution-success".to_string();
        successful.child_runtime_id = "runtime-pre-execution-success".to_string();
        successful.child_transaction_id = "transaction-pre-execution-success".to_string();
        successful.child_lease_id = "lease-pre-execution-success".to_string();
        successful.callback_request_id = "transaction-pre-execution-success".to_string();
        successful.effect_id = "runtime-pre-execution-success.message".to_string();
        successful.session_name = "child pre-execution success".to_string();
        successful.session_directory = workspace.join("child-success").display().to_string();
        std::fs::create_dir_all(&successful.session_directory).expect("successful child workspace");
        create_child_session(&parent, &successful).expect("create successful child session");
        let successful_task_id = "task-pre-execution-success";
        let successful_admission = child_admission_record(
            &successful,
            ChildReadySetIdentity::new(
                "mission-pre-execution-success",
                successful_task_id,
                "1".repeat(64),
                "2".repeat(64),
                "3".repeat(64),
                "4".repeat(64),
                "5".repeat(64),
                vec!["src/**".to_string()],
                Vec::new(),
                Vec::new(),
            ),
        );
        store
            .admit_child(&successful_admission)
            .expect("successful child admission");
        let service = ExecutionService::new();
        service
            .terminalize_admitted_child_pre_execution_failure(
                &state,
                &successful,
                successful_task_id,
                &anyhow::anyhow!("fixture enqueue failed after admission"),
            )
            .await
            .expect("terminalize child before callback publication");
        let terminal_child = read_session_snapshot(&successful.child_session_id)
            .expect("terminal child readback")
            .expect("terminal child");
        assert_eq!(
            terminal_child.lifecycle_projection.state,
            SessionState::Cancelled
        );
        assert!(
            terminal_child
                .lifecycle_projection
                .active_runtime_id
                .is_none()
        );
        assert!(
            admitted_pre_execution_failure_receipt_from_store(
                &store,
                &successful,
                successful_task_id,
            )
            .expect("successful terminal receipt readback")
            .is_some()
        );
        let replay = service
            .replay_admitted_pre_execution_failure_callback(&successful)
            .expect("terminal callback replay")
            .expect("published terminal callback");
        assert_eq!(replay.0["method"], "session.terminal_failure");
        assert_eq!(
            replay.0["payload"]["body"]["item"]["classification"],
            "PROVEN_ZERO_EFFECT"
        );
        assert_eq!(
            store
                .callbacks_for_replay()
                .expect("successful callback replay readback")
                .len(),
            1
        );
    }

    #[test]
    fn terminal_failure_and_cancellation_require_admission_and_remain_unsettled() {
        for terminal_state in [TerminalState::Failed, TerminalState::Cancelled] {
            let root = tempfile::tempdir().expect("temp lifecycle root");
            let (store, delivery) = durable_callback_fixture(root.path(), terminal_state);
            assert_eq!(
                publish_terminal_failure_callback_from_store(&store, delivery.clone())
                    .expect_err("unadmitted failure must fail closed")
                    .to_string(),
                "TERMINAL_CALLBACK_CHILD_ADMISSION_NOT_DURABLE:child-callback"
            );
            assert_eq!(
                store
                    .readback()
                    .expect("missing admission readback")
                    .intaken_callbacks,
                0
            );

            admit_callback_child(&store, &"a".repeat(64));
            let (transport, delivery) =
                publish_terminal_failure_callback_from_store(&store, delivery)
                    .expect("failure callback")
                    .expect("delegated failure callback");

            assert_eq!(transport["method"], "session.terminal_failure");
            assert_eq!(
                transport["payload"]["body"]["item"]["classification"],
                "UNSETTLED_EFFECT"
            );
            assert!(matches!(
                delivery.callback_effect_identity,
                Some(CallbackEffectIdentity::UnsettledEffect { .. })
            ));
            let readback = store.readback().expect("readback");
            assert_eq!(readback.pending_callbacks, 0);
            assert_eq!(readback.intaken_callbacks, 1);
            assert_eq!(readback.acknowledged_callbacks, 0);
            assert_eq!(readback.acknowledged_receipts, 0);
        }

        let root = tempfile::tempdir().expect("mismatched admission root");
        let (store, delivery) = durable_callback_fixture(root.path(), TerminalState::Failed);
        admit_callback_child(&store, &"b".repeat(64));
        assert_eq!(
            publish_terminal_failure_callback_from_store(&store, delivery)
                .expect_err("mismatched admission must fail closed")
                .to_string(),
            "TERMINAL_CALLBACK_ADMISSION_IDENTITY_MISMATCH:child-callback"
        );
        assert_eq!(
            store
                .readback()
                .expect("mismatch readback")
                .intaken_callbacks,
            0
        );
    }
}
