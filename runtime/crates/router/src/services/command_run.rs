//! Router-owned `command_run` execution.
//!
//! Runtime workers orchestrate turns, but shell/tool child processes are owned
//! here so aborting a runtime worker does not orphan process-tree cleanup.

use anyhow::{Context, Result, anyhow};
use code_tools::runtime::tool::CancellationToken;
use code_tools::commands::source_read::{
    SourceReadPreExecutionRejection, SourceReadRejectionKind, SourceReadTargetError,
};
use parking_lot::Mutex;
use runtime_contract::{CapsuleSourceReadBinding, CapsuleSourceReadContext};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicU64, AtomicUsize, Ordering},
};
use tura_path::command_receipts::ReceiptStore;
use tura_path::jspace::{JSpaceAdmissionCache, JSpaceError, JSpaceMatcher};

fn observation_owner(store: &ReceiptStore, runtime_id: Option<&str>) -> Option<String> {
    // A caller-bound receipt scope survives model continuations; a runtime ID does not.
    // Legacy unbound stores retain the narrower runtime-local behavior.
    store.observation_scope().or(runtime_id).map(str::to_owned)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommandRunRequest {
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub runtime_id: Option<String>,
    pub session_directory: PathBuf,
    pub arguments: Value,
    #[serde(default)]
    pub allowed_commands: Option<BTreeSet<String>>,
    #[serde(default)]
    pub command_env: BTreeMap<String, String>,
    #[serde(default)]
    pub sandbox: bool,
    #[serde(default)]
    pub jspace_contract: Option<Value>,
    #[serde(default)]
    pub capsule_source_read_context: Option<CapsuleSourceReadContext>,
}

#[cfg(test)]
const TEST_PANIC_AFTER_RUNNING_CLAIM: &str = "TURA_TEST_PANIC_AFTER_RUNNING_CLAIM";

#[cfg(test)]
#[derive(Debug, Clone)]
struct PanicCleanupGate {
    entered: Arc<tokio::sync::Semaphore>,
    release: Arc<tokio::sync::Semaphore>,
}

#[cfg(test)]
impl PanicCleanupGate {
    fn new() -> Self {
        Self {
            entered: Arc::new(tokio::sync::Semaphore::new(0)),
            release: Arc::new(tokio::sync::Semaphore::new(0)),
        }
    }
}

#[derive(Debug, Clone)]
pub struct CommandRunService {
    active: Arc<AtomicUsize>,
    active_by_session: Arc<Mutex<HashMap<String, usize>>>,
    cancellations: Arc<Mutex<HashMap<String, HashMap<u64, CancellationToken>>>>,
    next_cancellation_id: Arc<AtomicU64>,
    idle: Arc<tokio::sync::Notify>,
    jspace: JSpaceAdmissionCache,
    panic_cleanup_quarantine: Arc<Mutex<Vec<PendingPanicCleanup>>>,
    #[cfg(test)]
    panic_cleanup_gate: Arc<Mutex<Option<PanicCleanupGate>>>,
}

impl CommandRunService {
    pub fn new() -> Self {
        Self {
            active: Arc::new(AtomicUsize::new(0)),
            active_by_session: Arc::new(Mutex::new(HashMap::new())),
            cancellations: Arc::new(Mutex::new(HashMap::new())),
            next_cancellation_id: Arc::new(AtomicU64::new(1)),
            idle: Arc::new(tokio::sync::Notify::new()),
            jspace: JSpaceAdmissionCache::default(),
            panic_cleanup_quarantine: Arc::new(Mutex::new(Vec::new())),
            #[cfg(test)]
            panic_cleanup_gate: Arc::new(Mutex::new(None)),
        }
    }

    #[allow(
        dead_code,
        reason = "legacy in-process callers use the request-id path"
    )]
    pub async fn execute(&self, input: Value) -> Result<Value> {
        self.execute_with_request_id(input, None).await
    }

    pub async fn execute_with_request_id(
        &self,
        input: Value,
        request_id: Option<&str>,
    ) -> Result<Value> {
        self.execute_with_reservation(input, request_id, None).await
    }

    pub(crate) fn reserve_for_session(&self, session_id: Option<&str>) -> ActiveCommandRunGuard {
        ActiveCommandRunGuard::new(
            Arc::clone(&self.active),
            Arc::clone(&self.active_by_session),
            Arc::clone(&self.cancellations),
            Arc::clone(&self.next_cancellation_id),
            Arc::clone(&self.idle),
            session_id,
        )
    }

    pub(crate) async fn execute_with_reserved_session(
        &self,
        input: Value,
        request_id: Option<&str>,
        reservation: ActiveCommandRunGuard,
    ) -> Result<Value> {
        self.execute_with_reservation(input, request_id, Some(reservation))
            .await
    }

    async fn execute_with_reservation(
        &self,
        input: Value,
        request_id: Option<&str>,
        reservation: Option<ActiveCommandRunGuard>,
    ) -> Result<Value> {
        let request: CommandRunRequest =
            serde_json::from_value(input).context("invalid command_run router payload")?;
        let active =
            reservation.unwrap_or_else(|| self.reserve_for_session(request.session_id.as_deref()));
        if request.session_directory.as_os_str().is_empty() {
            return Err(anyhow!("command_run session_directory is required"));
        }
        let session_id = request.session_id.clone();
        let jspace_matcher = self
            .jspace
            .admit(
                session_id.as_deref().unwrap_or_default(),
                &request.session_directory,
                request.jspace_contract.as_ref(),
            )
            .map_err(|error| anyhow!(error.to_string()))?;
        let source_read_requested = contains_source_read(&request.arguments);
        let capsule_binding = match capsule_source_binding(&request, jspace_matcher.as_deref()) {
            Ok(binding) => binding,
            Err(error) => {
                return Ok(json!({
                    "status": "finished", "owner": "router", "session_id": session_id,
                    "runtime_id": request.runtime_id,
                    "execution_id": request_id.unwrap_or("command-run-legacy"),
                    "result": jspace_error_result(error),
                }));
            }
        };
        if jspace_matcher.is_none() && source_read_requested {
            return Ok(json!({
                "status": "finished",
                "owner": "router",
                "session_id": session_id,
                "runtime_id": request.runtime_id,
                "execution_id": request_id.unwrap_or("command-run-legacy"),
                "result": jspace_error_result(JSpaceError::new(
                    "JSPACE_SOURCE_READ_DENIED", "read", "source_read",
                    "source_read requires an admitted J-Space contract",
                )),
            }));
        }
        let source_read_root = if source_read_requested
            && let Some(matcher) = jspace_matcher.as_deref()
            && matcher.source_read_enabled()
        {
            match code_tools::commands::source_read::open_admitted_root(matcher.repo_root()) {
                Ok(root) => Some(Arc::new(root)),
                Err(error) => {
                    return Ok(json!({
                        "status": "finished",
                        "owner": "router",
                        "session_id": session_id,
                        "runtime_id": request.runtime_id,
                        "execution_id": request_id.unwrap_or("command-run-legacy"),
                        "result": jspace_error_result(JSpaceError::new(
                            "JSPACE_SOURCE_READ_PATH_DENIED", "read", "source_read", error,
                        )),
                    }));
                }
            }
        } else {
            None
        };
        if let Some(matcher) = jspace_matcher.as_deref()
            && let Err(error) = validate_jspace_arguments_with_capsule(matcher, &request.arguments, capsule_binding)
        {
            let (error, rejection_kind) = match error {
                JSpacePreflightError::Policy(error) => (error, None),
                JSpacePreflightError::SourceReadArguments(error, kind) => (error, Some(kind)),
            };
            let proof = match rejection_kind {
                Some(kind) => record_source_read_rejection(&request, request_id, kind, error.to_string())?,
                None => None,
            };
            let result = match proof {
                Some(proof) => json!({"results": [proof.failed_result()]}),
                None => {
                    // Best effort: a missing witness only keeps the denial uncertain downstream.
                    let _ = record_policy_denial(&request, request_id, &error);
                    jspace_error_result(error)
                }
            };
            return Ok(json!({
                "status": "finished",
                "owner": "router",
                "session_id": session_id,
                "runtime_id": request.runtime_id,
                "execution_id": request_id.unwrap_or("command-run-legacy"),
                "result": result,
            }));
        }
        let mut source_read_digests = BTreeMap::new();
        if let Some(matcher) = jspace_matcher.as_deref() {
            for command in code_tools::command_run::command_run_preflight_commands(&request.arguments)
                .map_err(|error| anyhow!(error))?
            {
                if code_tools::commands::canonical_command(&command.command) != "source_read" { continue; }
                let parsed = code_tools::commands::source_read::parse_command_line(&command.command_line)
                    .map_err(|error| anyhow!(error))?;
                if parsed.uses_admitted_digest() {
                    source_read_digests.insert(parsed.path().to_owned(),
                        admitted_source_digest(matcher, capsule_binding, Path::new(parsed.path()))?);
                }
            }
        }
        let execution_id = request_id
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| {
                request
                    .arguments
                    .get("execution_id")
                    .and_then(Value::as_str)
                    .unwrap_or("command-run-legacy")
            })
            .to_string();
        let mut arguments = request.arguments;
        if let Some(object) = arguments.as_object_mut() {
            object
                .entry("execution_id".to_string())
                .or_insert_with(|| Value::String(execution_id.clone()));
        }
        let (batch_execution_id, batch_call_ids) =
            code_tools::command_run::command_run_batch_identity(&arguments)
                .map_err(|error| anyhow!("COMMAND_RUN_BATCH_IDENTITY_INVALID:{error}"))?;
        let focused_verifiers = match super::focused_verifier::prepare_batch(
            &arguments,
            jspace_matcher.as_deref(),
            request.allowed_commands.as_ref(),
            &batch_call_ids,
        ) {
            Ok(value) => value,
            Err(error) => {
                return Ok(json!({
                    "status":"finished", "owner":"router", "session_id":session_id,
                    "runtime_id":request.runtime_id, "execution_id":execution_id,
                    "result":jspace_error_result(JSpaceError::new(
                        "JSPACE_VERIFIER_DENIED", "command", "focused_verifier", error,
                    )),
                }));
            }
        };
        let parent_shells = match super::parent_shell::prepare_batch(
            &arguments,
            jspace_matcher.as_deref(),
            &batch_call_ids,
        ) {
            Ok(value) => value,
            Err(error) => {
                return Ok(json!({
                    "status":"finished", "owner":"router", "session_id":session_id,
                    "runtime_id":request.runtime_id, "execution_id":execution_id,
                    "result":jspace_error_result(JSpaceError::new(
                        "JSPACE_PARENT_SHELL_DENIED", "command", "parent_shell", error,
                    )),
                }));
            }
        };
        let session_directory = request.session_directory;
        let receipt_store = Arc::new(
            ReceiptStore::open(&session_directory)
                .map_err(|error| anyhow!("COMMAND_RUN_RECEIPT_STORE_INVALID:{error}"))?,
        );
        #[cfg(test)]
        let worker_session_directory = session_directory.clone();
        let worker_session_id = session_id.clone();
        #[cfg(test)]
        let worker_call_ids = batch_call_ids.clone();
        let runtime_id = request.runtime_id;
        let source_observation_owner = observation_owner(&receipt_store, runtime_id.as_deref());
        code_tools::shell_executor::begin_command_run_batch(
            &receipt_store,
            &batch_execution_id,
            &batch_call_ids,
        )
        .map_err(|error| anyhow!("COMMAND_RUN_BATCH_ADMISSION_FAILED:{error}"))?;
        let worker_receipt_store = Arc::clone(&receipt_store);
        let worker_batch_execution_id = batch_execution_id.clone();
        let cleanup_receipt_store = Arc::clone(&receipt_store);
        let panic_cleanup_quarantine = Arc::clone(&self.panic_cleanup_quarantine);
        #[allow(unused_mut)]
        let mut command_env = request.command_env;
        #[cfg(test)]
        let panic_after_running_claim =
            command_env.remove(TEST_PANIC_AFTER_RUNNING_CLAIM).is_some();
        #[cfg(test)]
        let panic_cleanup_gate = self.panic_cleanup_gate.lock().clone();
        let supervisor = tokio::spawn(async move {
            let cancellation = active.cancellation_token();
            let worker_cancellation = cancellation.clone();
            let worker = tokio::spawn(async move {
                let execution = async move {
                    if let Some(matcher) = jspace_matcher.as_deref()
                        && let Err(error) = matcher.revalidate_mapped_source_reads()
                    {
                        return jspace_error_result(error);
                    }
                    if let Some(commands) = focused_verifiers {
                        return super::focused_verifier::execute_batch(
                            commands,
                            jspace_matcher
                                .as_deref()
                                .expect("preflight admitted verifier"),
                            &session_directory,
                            &worker_batch_execution_id,
                            worker_cancellation,
                            worker_receipt_store,
                        )
                        .await;
                    }
                    if let Some(commands) = parent_shells {
                        return super::parent_shell::execute_batch(
                            commands,
                            jspace_matcher
                                .as_deref()
                                .expect("preflight admitted parent shell"),
                            &session_directory,
                            &worker_batch_execution_id,
                            worker_cancellation,
                            worker_receipt_store,
                        )
                        .await;
                    }
                    let mapped_source_reads = jspace_matcher.as_deref().and_then(|matcher| {
                        (!matcher.mapped_source_reads().is_empty()).then(|| {
                            (matcher.repo_root().to_path_buf(), matcher.mapped_source_reads().to_vec())
                        })
                    });
                    let patch_readback_paths = jspace_matcher.as_deref().map(|matcher| {
                        admitted_patch_readback_paths(&arguments, matcher)
                    }).unwrap_or_default();
                    code_tools::registry::with_command_environment(
                        command_env,
                        code_tools::command_run::execute_async_value_with_source_digest_admission(
                            arguments,
                            session_directory,
                            request.allowed_commands,
                            worker_session_id,
                            request.sandbox,
                            worker_cancellation,
                            source_read_root,
                            mapped_source_reads,
                            Some(worker_receipt_store),
                            patch_readback_paths,
                            source_read_digests,
                            source_observation_owner,
                        ),
                    )
                    .await
                };
                #[cfg(test)]
                let output = if panic_after_running_claim {
                    let execution = execution;
                    tokio::pin!(execution);
                    tokio::select! {
                        output = &mut execution => output,
                        _ = Self::wait_for_running_command_claim(
                            &worker_session_directory,
                            &worker_call_ids,
                        ) => panic!("injected command worker panic after running claim"),
                    }
                } else {
                    execution.await
                };
                #[cfg(not(test))]
                let output = execution.await;
                output
            });

            match worker.await {
                Ok(output) => {
                    code_tools::shell_executor::complete_command_run_batch(
                        &cleanup_receipt_store,
                        &batch_execution_id,
                        &batch_call_ids,
                    )
                    .map_err(|error| anyhow!("COMMAND_RUN_BATCH_TERMINALIZATION_FAILED:{error}"))?;
                    drop(active);
                    Ok(json!({
                        "status": "finished",
                        "owner": "router",
                        "session_id": session_id,
                        "runtime_id": runtime_id,
                        "execution_id": execution_id,
                        "result": output,
                    }))
                }
                Err(worker_error) => {
                    cancellation.cancel();
                    #[cfg(test)]
                    if let Some(gate) = panic_cleanup_gate {
                        gate.entered.add_permits(1);
                        gate.release
                            .acquire()
                            .await
                            .expect("panic cleanup test gate")
                            .forget();
                    }
                    match code_tools::shell_executor::terminalize_interrupted_command_run_claims(
                        &cleanup_receipt_store,
                        &batch_execution_id,
                        &batch_call_ids,
                    )
                    .await
                    {
                        Ok(_) => {
                            drop(active);
                            Err(anyhow!("ROUTER_COMMAND_RUN_WORKER_FAILED:{worker_error}"))
                        }
                        Err(cleanup_error) => {
                            panic_cleanup_quarantine.lock().push(PendingPanicCleanup {
                                receipt_store: cleanup_receipt_store,
                                batch_execution_id,
                                batch_call_ids,
                                active,
                            });
                            Err(anyhow!(
                                "ROUTER_COMMAND_RUN_PANIC_CLEANUP_INCOMPLETE:{worker_error}:{cleanup_error}"
                            ))
                        }
                    }
                }
            }
        });
        supervisor
            .await
            .map_err(|error| anyhow!("ROUTER_COMMAND_RUN_SUPERVISOR_FAILED:{error}"))?
    }

    pub fn active_count(&self) -> usize {
        self.active.load(Ordering::SeqCst)
    }

    pub fn active_count_for_session(&self, session_id: &str) -> usize {
        self.active_by_session
            .lock()
            .get(session_id)
            .copied()
            .unwrap_or(0)
    }

    pub async fn wait_for_session_idle(&self, session_id: &str) {
        loop {
            let notified = self.idle.notified();
            self.retry_panic_cleanups_for_session(session_id).await;
            if self.active_count_for_session(session_id) == 0 {
                return;
            }
            tokio::select! {
                _ = notified => {},
                _ = tokio::time::sleep(std::time::Duration::from_secs(1)) => {},
            }
        }
    }

    async fn retry_panic_cleanups_for_session(&self, session_id: &str) {
        let pending = {
            let mut quarantine = self.panic_cleanup_quarantine.lock();
            let mut pending = Vec::new();
            let mut index = 0;
            while index < quarantine.len() {
                if quarantine[index].active.session_id.as_deref() == Some(session_id) {
                    pending.push(quarantine.swap_remove(index));
                } else {
                    index += 1;
                }
            }
            pending
        };
        let mut failed = Vec::new();
        for pending_cleanup in pending {
            if code_tools::shell_executor::terminalize_interrupted_command_run_claims(
                &pending_cleanup.receipt_store,
                &pending_cleanup.batch_execution_id,
                &pending_cleanup.batch_call_ids,
            )
            .await
            .is_err()
            {
                failed.push(pending_cleanup);
            }
        }
        self.panic_cleanup_quarantine.lock().extend(failed);
    }

    pub fn cancel_session(&self, session_id: &str) -> usize {
        let tokens = self
            .cancellations
            .lock()
            .get(session_id)
            .map(|tokens| tokens.values().cloned().collect::<Vec<_>>())
            .unwrap_or_default();
        for token in &tokens {
            token.cancel();
        }
        tokens.len()
    }

    #[cfg(test)]
    fn install_panic_cleanup_gate(&self) -> PanicCleanupGate {
        let gate = PanicCleanupGate::new();
        *self.panic_cleanup_gate.lock() = Some(gate.clone());
        gate
    }

    #[cfg(test)]
    fn jspace_admissions(&self) -> usize {
        self.jspace.admissions()
    }

    #[cfg(test)]
    async fn wait_for_running_command_claim(session_directory: &Path, call_ids: &[String]) {
        let directory = session_directory.join(".tura/run/command_receipts");
        tokio::time::timeout(std::time::Duration::from_secs(10), async {
            loop {
                let running = std::fs::read_dir(&directory)
                    .ok()
                    .into_iter()
                    .flatten()
                    .filter_map(Result::ok)
                    .filter_map(|entry| std::fs::read(entry.path()).ok())
                    .filter_map(|raw| serde_json::from_slice::<Value>(&raw).ok())
                    .any(|claim| {
                        claim
                            .get("call_id")
                            .and_then(Value::as_str)
                            .is_some_and(|call_id| {
                                call_ids.iter().any(|expected| expected == call_id)
                            })
                            && claim.get("state").and_then(Value::as_str) == Some("running")
                    });
                if running {
                    return;
                }
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("test command claim did not reach running state");
    }
}

#[derive(Debug)]
enum JSpacePreflightError {
    Policy(JSpaceError),
    SourceReadArguments(JSpaceError, SourceReadRejectionKind),
}

impl From<JSpaceError> for JSpacePreflightError {
    fn from(error: JSpaceError) -> Self { Self::Policy(error) }
}

fn record_source_read_rejection(
    request: &CommandRunRequest, request_id: Option<&str>, kind: SourceReadRejectionKind,
    error_message: String,
) -> Result<Option<SourceReadPreExecutionRejection>> {
    let Some(contract) = request.jspace_contract.as_ref() else { return Ok(None); };
    let Some(authorization) = code_tools::commands::source_read::source_read_recovery_authorization(contract)
        else { return Ok(None); };
    let Some(commands) = request.arguments["commands"].as_array().filter(|commands| commands.len() == 1)
        else { return Ok(None); };
    if request.allowed_commands.as_ref().is_some_and(|allowed| !allowed.contains("source_read")) {
        return Ok(None);
    }
    let (Some(session_id), Some(runtime_id)) = (request.session_id.as_deref(), request.runtime_id.as_deref())
        else { return Ok(None); };
    // No anonymous/legacy identity and no inference from the malformed JSON.
    let Some(execution_id) = request_id.map(str::trim).filter(|id| !id.is_empty())
        .or_else(|| request.arguments["execution_id"].as_str().filter(|id| !id.trim().is_empty()))
        else { return Ok(None); };
    let mut arguments = request.arguments.clone();
    arguments.as_object_mut().expect("commands object")
        .entry("execution_id").or_insert_with(|| json!(execution_id));
    let (execution_id, call_ids) = code_tools::command_run::command_run_batch_identity(&arguments)
        .map_err(|error| anyhow!("COMMAND_RUN_BATCH_IDENTITY_INVALID:{error}"))?;
    if call_ids.len() != 1 { return Ok(None); }
    let Some(proof) = SourceReadPreExecutionRejection::new(session_id, runtime_id, &execution_id,
        &call_ids[0], authorization, &commands[0], kind, error_message) else { return Ok(None); };
    let store = ReceiptStore::open(&request.session_directory)
        .map_err(|error| anyhow!("COMMAND_RUN_RECEIPT_STORE_INVALID:{error}"))?;
    proof.publish(&store).map_err(|error| anyhow!("SOURCE_READ_REJECTION_RECEIPT_INVALID:{error}"))?;
    Ok(Some(proof))
}

/// Publish the router's witness that J-Space refused this one-command request before dispatch.
/// Native streaming sends each command alone, so the denial covers exactly one call.
fn record_policy_denial(request: &CommandRunRequest, request_id: Option<&str>, error: &JSpaceError) -> Result<()> {
    let Some(authorization) = request.jspace_contract.as_ref()
        .and_then(|contract| contract["authorization_semantic_sha256"].as_str()) else { return Ok(()); };
    let Some(commands) = request.arguments["commands"].as_array().filter(|commands| commands.len() == 1)
        else { return Ok(()); };
    let (Some(session_id), Some(runtime_id)) = (request.session_id.as_deref(), request.runtime_id.as_deref())
        else { return Ok(()); };
    let Some(execution_id) = request_id.map(str::trim).filter(|id| !id.is_empty())
        .or_else(|| request.arguments["execution_id"].as_str().filter(|id| !id.trim().is_empty()))
        else { return Ok(()); };
    let mut arguments = request.arguments.clone();
    arguments.as_object_mut().expect("commands object")
        .entry("execution_id").or_insert_with(|| json!(execution_id));
    let Ok((execution_id, call_ids)) = code_tools::command_run::command_run_batch_identity(&arguments)
        else { return Ok(()); };
    let [call_id] = call_ids.as_slice() else { return Ok(()); };
    let Some(denial) = code_tools::command_run::JSpacePolicyDenial::new(session_id, runtime_id, &execution_id,
        call_id, authorization, &commands[0], error.code(), error.operation(), error.target(), &error.to_string())
        else { return Ok(()); };
    let store = ReceiptStore::open(&request.session_directory)
        .map_err(|error| anyhow!("COMMAND_RUN_RECEIPT_STORE_INVALID:{error}"))?;
    denial.publish(&store).map_err(|error| anyhow!("JSPACE_POLICY_DENIAL_RECEIPT_INVALID:{error}"))?;
    Ok(())
}

#[cfg(test)]
fn validate_jspace_arguments(
    matcher: &JSpaceMatcher,
    arguments: &Value,
) -> Result<(), JSpacePreflightError> {
    validate_jspace_arguments_with_capsule(matcher, arguments, None)
}

fn capsule_source_binding<'a>(
    request: &'a CommandRunRequest,
    matcher: Option<&JSpaceMatcher>,
) -> Result<Option<&'a CapsuleSourceReadBinding>, JSpaceError> {
    let Some(context) = request.capsule_source_read_context.as_ref() else { return Ok(None); };
    let invalid = || JSpaceError::new(
        "JSPACE_CAPSULE_SOURCE_BINDING_INVALID", "read", "source_read",
        "capsule source expectation does not match this runtime and admitted contract",
    );
    if context.session_id.trim().is_empty() || context.runtime_id.trim().is_empty()
        || request.session_id.as_deref() != Some(context.session_id.as_str())
        || request.runtime_id.as_deref() != Some(context.runtime_id.as_str())
    {
        return Err(invalid());
    }
    let matcher = matcher.ok_or_else(invalid)?;
    let contract = request.jspace_contract.as_ref().ok_or_else(invalid)?;
    context.binding.validate_against(contract, matcher.repo_root()).map_err(|_| invalid())?;
    Ok(Some(&context.binding))
}

fn admitted_source_digest(
    matcher: &JSpaceMatcher,
    capsule: Option<&CapsuleSourceReadBinding>,
    relative: &Path,
) -> Result<String, JSpaceError> {
    match matcher.admitted_source_read_digest(relative) {
        Ok(digest) => Ok(digest),
        Err(error) => {
            let Some(binding) = capsule else { return Err(error); };
            let Some(name) = relative.to_str() else { return Err(error); };
            if !matcher.source_read_enabled() || relative.is_absolute() || name.contains('\\')
                || relative.components().any(|part| !matches!(part, std::path::Component::Normal(_)))
                || !matcher.scope_projection().read_scopes.iter().any(|scope| scope == name)
                || !matcher.scope_projection().write_scopes.is_empty()
                || matcher.mapped_source_read_binding(relative).is_some()
            {
                return Err(error);
            }
            let Some(digest) = binding.digest(name) else { return Err(error); };
            matcher.check_source_read(&matcher.repo_root().join(relative))?;
            Ok(digest.to_owned())
        }
    }
}

fn validate_jspace_arguments_with_capsule(
    matcher: &JSpaceMatcher,
    arguments: &Value,
    capsule: Option<&CapsuleSourceReadBinding>,
) -> Result<(), JSpacePreflightError> {
    matcher.revalidate_mapped_source_reads()?;
    let commands =
        code_tools::command_run::command_run_preflight_commands(arguments).map_err(|error| {
            JSpaceError::new("JSPACE_COMMAND_PAYLOAD_INVALID", "command", "", error)
        })?;
    if matcher.parent_shell_admitted() {
        let routed = commands
            .iter()
            .filter(|command| matcher.parent_shell_routes(&command.command, &command.command_line))
            .count();
        if routed != 0 && routed != commands.len() {
            return Err(JSpaceError::new(
                "JSPACE_PARENT_SHELL_BATCH_MIXED",
                "command",
                "parent_shell",
                "shell lines run in the parent process: send them in their own command_run call, \
                 separate from apply_patch, source_read and template commands",
            ).into());
        }
    }
    for command in commands {
        let command_type = command.command.as_str();
        let command_line = command.command_line.as_str();
        let canonical_command = code_tools::commands::canonical_command(command_type);
        if canonical_command == "focused_verifier" {
            // Typed payload and exact grants are checked for the whole batch
            // before receipt admission, not through shell template matching.
            continue;
        }
        if canonical_command == "source_read" {
            if !matcher.source_read_enabled() {
                return Err(JSpaceError::new(
                    "JSPACE_SOURCE_READ_DENIED",
                    "read",
                    "source_read",
                    "source_read is not explicitly granted",
                ).into());
            }
            if command.workdir.is_some() {
                return Err(JSpaceError::new(
                    "JSPACE_SOURCE_READ_PATH_DENIED",
                    "read",
                    "source_read",
                    "source_read does not accept a workdir override",
                ).into());
            }
            let request = code_tools::commands::source_read::parse_command_line_typed(command_line)
                .map_err(|error| {
                    let policy = JSpaceError::new("JSPACE_SOURCE_READ_INVALID", "read", "source_read", error.to_string());
                    match error.rejection_kind() {
                        Some(kind) => JSpacePreflightError::SourceReadArguments(policy, kind),
                        None => JSpacePreflightError::Policy(policy),
                    }
                })?;
            if request.uses_admitted_digest() {
                admitted_source_digest(matcher, capsule, Path::new(request.path()))?;
            }
            if matcher.mapped_source_read_binding(Path::new(request.path())).is_some() {
                matcher.check_source_read(Path::new(request.path()))?;
                continue;
            }
            let target = match code_tools::commands::source_read::target_path_typed(
                matcher.repo_root(), &request,
            ) {
                Ok(target) => target,
                Err(SourceReadTargetError::NotFound) => {
                    // Missing data is an ordinary read failure only within an explicit read grant.
                    matcher.check_source_read(&matcher.repo_root().join(request.path()))?;
                    continue;
                }
                Err(error) => return Err(JSpaceError::new(
                    "JSPACE_SOURCE_READ_PATH_DENIED", "read", request.path(), error.to_string(),
                ).into()),
            };
            matcher.check_source_read(&target)?;
            continue;
        }
        matcher.check_command(command_type, command_line)?;
        // An apply_patch entry without text changes nothing and needs no admission.
        if canonical_command == "apply_patch" && !command_line.trim().is_empty() {
            let changes = code_tools::commands::apply_patch::jspace_changes(command_line)
                .map_err(|error| JSpaceError::new("JSPACE_PATCH_MALFORMED", "modify", "", error))?;
            for (kind, path, move_path) in changes {
                let operation = match kind.as_str() {
                    "add" => "create",
                    "update" => "modify",
                    "delete" => "delete",
                    _ => {
                        return Err(JSpaceError::new(
                            "JSPACE_UNKNOWN_OPERATION",
                            "modify",
                            &path,
                            format!("unknown apply_patch change kind {kind}"),
                        ).into());
                    }
                };
                matcher.check_path(operation, Path::new(&path))?;
                if let Some(move_path) = move_path {
                    matcher.check_path("create", Path::new(&move_path))?;
                }
            }
        } else if let Some(workdir) = command.workdir.as_deref() {
            matcher.ensure_in_root(Path::new(workdir), "command")?;
        }
    }
    Ok(())
}

fn contains_source_read(arguments: &Value) -> bool {
    code_tools::command_run::command_run_preflight_commands(arguments).is_ok_and(|commands| {
        commands.iter().any(|command| {
            code_tools::commands::canonical_command(&command.command) == "source_read"
        })
    })
}

fn admitted_patch_readback_paths(arguments: &Value, matcher: &JSpaceMatcher) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    let Ok(commands) = code_tools::command_run::command_run_preflight_commands(arguments) else {
        return paths;
    };
    for command in commands {
        if code_tools::commands::canonical_command(&command.command) != "apply_patch" {
            continue;
        }
        let Ok(changes) = code_tools::commands::apply_patch::jspace_changes(&command.command_line) else {
            continue;
        };
        for (kind, path, moved) in changes {
            if kind != "update" || moved.is_some() { continue; }
            let line = json!({"path":path,"start_line":1,"end_line":1}).to_string();
            let Ok(request) = code_tools::commands::source_read::parse_command_line(&line) else {
                continue;
            };
            let Ok(target) = code_tools::commands::source_read::target_path(matcher.repo_root(), &request) else {
                continue;
            };
            if matcher.check_source_read(&target).is_ok() && !paths.contains(&target) {
                paths.push(target);
            }
        }
    }
    paths
}

fn jspace_error_result(error: JSpaceError) -> Value {
    json!({
        "results": [{
            "success": false,
            "command_type": "jspace",
            "error": error.to_string(),
            "jspace_error_code": error.code(),
            "operation": error.operation(),
            "target": error.target(),
            "effect_state": "not_started",
            "mutation_count": 0,
            "authority_effect": "none",
            "delivery_state": "deterministic_policy_denial",
            "replayable": true,
        }]
    })
}

#[derive(Debug)]
pub(crate) struct ActiveCommandRunGuard {
    active: Arc<AtomicUsize>,
    active_by_session: Arc<Mutex<HashMap<String, usize>>>,
    cancellations: Arc<Mutex<HashMap<String, HashMap<u64, CancellationToken>>>>,
    cancellation_id: Option<u64>,
    cancellation: CancellationToken,
    idle: Arc<tokio::sync::Notify>,
    session_id: Option<String>,
}

#[derive(Debug)]
struct PendingPanicCleanup {
    receipt_store: Arc<ReceiptStore>,
    batch_execution_id: String,
    batch_call_ids: Vec<String>,
    active: ActiveCommandRunGuard,
}

impl ActiveCommandRunGuard {
    fn new(
        active: Arc<AtomicUsize>,
        active_by_session: Arc<Mutex<HashMap<String, usize>>>,
        cancellations: Arc<Mutex<HashMap<String, HashMap<u64, CancellationToken>>>>,
        next_cancellation_id: Arc<AtomicU64>,
        idle: Arc<tokio::sync::Notify>,
        session_id: Option<&str>,
    ) -> Self {
        active.fetch_add(1, Ordering::SeqCst);
        let session_id = session_id
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string);
        let cancellation = CancellationToken::new();
        let cancellation_id = session_id.as_ref().map(|session_id| {
            *active_by_session
                .lock()
                .entry(session_id.clone())
                .or_insert(0) += 1;
            let cancellation_id = next_cancellation_id.fetch_add(1, Ordering::SeqCst);
            cancellations
                .lock()
                .entry(session_id.clone())
                .or_default()
                .insert(cancellation_id, cancellation.clone());
            cancellation_id
        });
        Self {
            active,
            active_by_session,
            cancellations,
            cancellation_id,
            cancellation,
            idle,
            session_id,
        }
    }

    fn cancellation_token(&self) -> CancellationToken {
        self.cancellation.clone()
    }
}

impl Drop for ActiveCommandRunGuard {
    fn drop(&mut self) {
        self.active.fetch_sub(1, Ordering::SeqCst);
        if let Some(session_id) = self.session_id.as_ref() {
            let mut active = self.active_by_session.lock();
            if let Some(count) = active.get_mut(session_id) {
                *count -= 1;
                if *count == 0 {
                    active.remove(session_id);
                }
            }
            drop(active);
            if let Some(cancellation_id) = self.cancellation_id {
                let mut cancellations = self.cancellations.lock();
                if let Some(tokens) = cancellations.get_mut(session_id) {
                    tokens.remove(&cancellation_id);
                    if tokens.is_empty() {
                        cancellations.remove(session_id);
                    }
                }
            }
        }
        self.idle.notify_waiters();
    }
}

impl Default for CommandRunService {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::{CommandRunRequest, CommandRunService, validate_jspace_arguments};
    use serde_json::{Value, json};
    use std::collections::BTreeSet;
    use std::path::{Path, PathBuf};
    use std::time::{Duration, Instant};
    use tura_path::jspace::{authorization_semantic_sha256, semantic_sha256};

    const ACTIVE_FIXTURE_DELAY_MS: u64 = 1200;
    const CONCURRENT_FIXTURE_DELAY_MS: u64 = 3000;
    const READ_ONLY_FIXTURE_TIMEOUT_MS: u64 = 30000;

    fn canonical_tempdir() -> tempfile::TempDir {
        let temp_root = std::env::temp_dir()
            .canonicalize()
            .expect("canonical temporary root");
        tempfile::Builder::new()
            .tempdir_in(temp_root)
            .expect("temporary workspace")
    }

    fn jspace_contract(root: &Path) -> Value {
        let mut contract = json!({
            "schema_version": "jspace_contract_v2",
            "repo_root": root,
            "dcf_generation": {
                "repo_root": root,
                "generation_id": "generation-test",
                "required_domain_bindings": {
                    "surface-map": {
                        "required_domains": ["surface"],
                        "source_fingerprints": {"surface": "surface-a"}
                    }
                }
            },
            "provenance": {"matched_surface_ids": ["surface-test"]},
            "matched_surface_ids": ["surface-test"],
            "read_scopes": ["src/**"],
            "write_scopes": ["src/**"],
            "allowed_operations": ["read", "create", "modify", "command"],
            "denied_operations": ["network", "install", "system_mutation"],
            "command_templates": [{
                "argv": ["git", "status", "--short"],
                "effects": ["read"],
                "targets": []
            }],
            "focused_verifiers": [],
            "declared_targets": ["src/main.rs"],
            "expansion": {
                "mode": "exact_target_only",
                "error_code": "JSPACE_EXPANSION_REQUIRED",
                "mutation_on_expansion": false
            }
        });
        contract["authorization_semantic_sha256"] =
            Value::String(authorization_semantic_sha256(&contract).expect("authorization digest"));
        contract["content_sha256"] = Value::String(semantic_sha256(&contract));
        contract
    }

    fn reseal_jspace(mut contract: Value) -> Value {
        contract
            .as_object_mut()
            .expect("contract object")
            .remove("content_sha256");
        contract["authorization_semantic_sha256"] =
            Value::String(authorization_semantic_sha256(&contract).expect("authorization digest"));
        contract["content_sha256"] = Value::String(semantic_sha256(&contract));
        contract
    }

    #[cfg(unix)]
    fn capsule_digest_fixture() -> (tempfile::TempDir, Value, Value, String) {
        use runtime_contract::{TaskContextCapsule, CapsuleSourceReadContext,
            READONLY_SOURCE_OUTLINE_MARKER, READONLY_SOURCE_OUTLINE_NOTICE,
            task_context_semantic_sha256_v1};
        use sha2::{Digest, Sha256};
        let tree = canonical_tempdir();
        std::fs::create_dir(tree.path().join("src")).unwrap();
        std::fs::write(tree.path().join("src/one.py"), "value = 1\n").unwrap();
        std::fs::write(tree.path().join("src/outside.py"), "outside = 2\n").unwrap();
        let digest = format!("{:x}", Sha256::digest(b"value = 1\n"));
        let mut contract = jspace_contract(tree.path());
        contract["source_read"] = json!(true);
        contract["read_scopes"] = json!(["src/one.py"]);
        contract["write_scopes"] = json!([]);
        contract["declared_targets"] = json!([]);
        contract["allowed_operations"] = json!(["read", "command"]);
        contract["command_templates"] = json!([]);
        let contract = reseal_jspace(contract);
        let outline = json!({"kind":"readonly_source_outline", "notice":READONLY_SOURCE_OUTLINE_NOTICE,
            "files":[{"path":"src/one.py", "source_sha256":digest, "definitions":[]}]});
        let mut capsule = json!({"schema_version":"task_context_capsule_v1",
            "mission":{"mission_id":"capsule-source", "mode":"RESEARCH", "objective":"read",
                "current_predicate":"source preimage"},
            "context_summary":format!("Bounded source{READONLY_SOURCE_OUTLINE_MARKER}{outline}"),
            "dcf_generation":contract["dcf_generation"], "surface":{"repo_root":tree.path()},
            "authority":{}, "evidence_refs":[], "focused_verifiers":[],
            "jspace_semantic_sha256":contract["authorization_semantic_sha256"]});
        capsule["semantic_sha256"] = json!(task_context_semantic_sha256_v1(&capsule).unwrap());
        let capsule = TaskContextCapsule::from_value(capsule).unwrap();
        let binding = capsule.readonly_source_read_binding(&contract, tree.path()).unwrap().unwrap();
        let context = serde_json::to_value(CapsuleSourceReadContext {
            session_id:"capsule-session".into(), runtime_id:"capsule-runtime".into(), binding,
        }).unwrap();
        (tree, contract, context, digest)
    }

    #[cfg(unix)]
    fn capsule_digest_input(root: &Path, contract: &Value, context: &Value, selector: Value, id: &str) -> Value {
        json!({"session_id":"capsule-session", "runtime_id":"capsule-runtime",
            "session_directory":root, "jspace_contract":contract, "capsule_source_read_context":context,
            "allowed_commands":["source_read"], "arguments":{"execution_id":id, "commands":[{
                "command_type":"source_read", "command_line":selector.to_string()}]}})
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn capsule_source_digest_first_read_matches_literal_and_rejects_stale_buffer() {
        let (tree, contract, context, digest) = capsule_digest_fixture();
        let service = CommandRunService::new();
        let reference = json!({"path":"src/one.py", "start_line":1, "end_line":1,
            "expected_sha256_ref":"admitted"});
        let first = service.execute(capsule_digest_input(tree.path(), &contract, &context,
            reference.clone(), "capsule-first")).await.unwrap();
        let literal = service.execute(capsule_digest_input(tree.path(), &contract, &context,
            json!({"path":"src/one.py", "start_line":1,"end_line":1,"expected_sha256":digest}),
            "capsule-literal")).await.unwrap();
        assert_eq!(first["result"]["results"][0]["success"], true, "{first}");
        assert_eq!(first["result"]["results"][0]["output"]["stdout"], "1: value = 1\n");
        assert_eq!(first["result"]["results"][0]["output"]["source_sha256"], digest);
        assert_eq!(first["result"]["results"][0]["output"]["stdout"], literal["result"]["results"][0]["output"]["stdout"]);
        std::fs::write(tree.path().join("src/one.py"), "changed = 3\n").unwrap();
        let stale = service.execute(capsule_digest_input(tree.path(), &contract, &context,
            reference, "capsule-stale")).await.unwrap();
        assert_eq!(stale["result"]["results"][0]["success"], false, "{stale}");
        assert!(stale.to_string().contains("SOURCE_READ_SHA256_MISMATCH"), "{stale}");
        assert_eq!(stale["result"]["results"][0]["output"]["stdout"], "");
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn capsule_source_digest_never_repins_wrong_literal_or_accepts_model_context() {
        let (tree, contract, context, _) = capsule_digest_fixture();
        let service = CommandRunService::new();
        for length in [64, 67] {
            let response = service.execute(capsule_digest_input(tree.path(), &contract, &context,
                json!({"path":"src/one.py", "start_line":1,"end_line":1,"expected_sha256":"a".repeat(length)}),
                &format!("capsule-wrong-{length}"))).await.unwrap();
            assert_ne!(response["result"]["results"][0]["success"], true, "{response}");
            assert!(!response.to_string().contains("1: value = 1"), "{response}");
        }
        let mut model_context = capsule_digest_input(tree.path(), &contract, &Value::Null,
            json!({"path":"src/one.py", "start_line":1,"end_line":1,"expected_sha256_ref":"admitted"}),
            "capsule-model-spoof");
        model_context["arguments"]["capsule_source_read_context"] = context;
        let denied = service.execute(model_context).await.unwrap();
        assert!(denied.to_string().contains("JSPACE_SOURCE_DIGEST_REFERENCE_UNAVAILABLE"), "{denied}");
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn capsule_source_digest_rejects_foreign_changed_writable_mapped_and_outside_contexts() {
        let (tree, contract, context, _) = capsule_digest_fixture();
        let selector = json!({"path":"src/one.py", "start_line":1,"end_line":1,"expected_sha256_ref":"admitted"});
        for case in 0..7 {
            let mut changed = contract.clone();
            let mut foreign = context.clone();
            let mut selector = selector.clone();
            match case {
                0 => foreign["session_id"] = json!("foreign-session"),
                1 => foreign["runtime_id"] = json!("foreign-runtime"),
                2 => foreign["binding"]["repo_root"] = json!("/foreign"),
                3 => changed["write_scopes"] = json!(["src/one.py"]),
                4 => changed["read_scopes"] = json!(["src/outside.py"]),
                5 => changed["mapped_source_reads"] = json!([{}]),
                _ => selector["path"] = json!("src/outside.py"),
            }
            if changed != contract && case != 5 { changed = reseal_jspace(changed); }
            let response = CommandRunService::new().execute(capsule_digest_input(tree.path(), &changed,
                &foreign, selector, &format!("capsule-denied-{case}"))).await;
            match response {
                Ok(response) => {
                    assert_ne!(response["result"]["results"][0]["success"], true, "case {case}: {response}");
                    assert!(!response.to_string().contains("1: value = 1"), "case {case}: {response}");
                }
                Err(error) => assert!(error.to_string().contains("JSPACE_"), "case {case}: {error}"),
            }
        }
    }

    #[cfg(unix)]
    fn admitted_digest_fixture() -> (tempfile::TempDir, tempfile::TempDir, Value) {
        use sha2::{Digest, Sha256};
        use std::os::unix::fs::PermissionsExt;
        let tree = canonical_tempdir();
        let artifacts = canonical_tempdir();
        let root = tree.path();
        std::fs::create_dir(root.join("src")).unwrap();
        std::fs::write(root.join("src/main.rs"), "before\n").unwrap();
        std::fs::write(root.join("src/edit.rs"), "before\n").unwrap();
        let binary = root.join("runner");
        std::fs::write(&binary, [0xfe, 0xed, 0xfa, 0xcf, 1, 2, 3, 4]).unwrap();
        std::fs::set_permissions(&binary, std::fs::Permissions::from_mode(0o755)).unwrap();
        let mut contract = jspace_contract(root);
        contract["read_scopes"] = json!(["src/main.rs"]);
        contract["write_scopes"] = json!(["src/edit.rs"]);
        contract["declared_targets"] = json!(["src/edit.rs"]);
        contract["command_templates"] = json!([]);
        contract["source_read"] = json!(true);
        contract["verifier_artifact_root"] = json!(artifacts.path());
        contract["verifier_commands"] = json!([{
            "argv":[binary,root.join("src/main.rs")],
            "executable_sha256":format!("{:x}",Sha256::digest(std::fs::read(&binary).unwrap())),
            "pinned_files":[{"path":root.join("src/main.rs"),
                "sha256":format!("{:x}",Sha256::digest(b"before\n"))}],
            "timeout_seconds":30,"scratch_root":artifacts.path().join("scratch"),"network":false
        }]);
        (tree, artifacts, reseal_jspace(contract))
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn observed_digest_survives_model_runtime_rollover_only_in_same_bound_store() {
        use code_tools::runtime::tool::{ToolContext, ToolCall, ToolPayload, ToolHandler};
        use code_tools::commands::source_read::{SourceReadHandler, open_admitted_root};
        use tura_path::command_receipts::{ReceiptBinding, ReceiptStore};
        use std::sync::Arc;
        let workspace = canonical_tempdir();
        let state = canonical_tempdir();
        let foreign_state = canonical_tempdir();
        std::fs::write(workspace.path().join("file"), "unchanged\n").unwrap();
        let binding = ReceiptBinding::new(state.path(), workspace.path());
        let first = ReceiptStore::open_bound(workspace.path(), Some(&binding)).unwrap();
        let reopened = ReceiptStore::open_existing_bound(workspace.path(), Some(&binding)).unwrap();
        let foreign_binding = ReceiptBinding::new(foreign_state.path(), workspace.path());
        let foreign = ReceiptStore::open_bound(workspace.path(), Some(&foreign_binding)).unwrap();
        let owner_a = super::observation_owner(&first, Some("runtime-round-one")).unwrap();
        let owner_b = super::observation_owner(&reopened, Some("runtime-round-two")).unwrap();
        assert_eq!(owner_a, owner_b);
        assert_ne!(owner_a, super::observation_owner(&foreign, Some("runtime-round-one")).unwrap());
        let context = |store: ReceiptStore, runtime: &str| {
            let owner = super::observation_owner(&store, Some(runtime));
            ToolContext::new(workspace.path().to_path_buf())
                .with_receipt_store(Some(Arc::new(store)))
                .with_source_read_root(Some(Arc::new(open_admitted_root(workspace.path()).unwrap())))
                .with_source_observation_owner(owner)
        };
        let call = |reference: bool| {
            let mut arguments = json!({"path":"file", "start_line":1, "end_line":1});
            if reference { arguments["expected_sha256_ref"] = json!("observed"); }
            ToolCall {tool_name:"source_read".to_owned(), call_id:"read".to_owned(),
                payload:ToolPayload::Function {arguments}}
        };
        assert_eq!(SourceReadHandler.handle(call(false), context(first, "runtime-round-one")).await.unwrap().success, Some(true));
        assert_eq!(SourceReadHandler.handle(call(true), context(reopened, "runtime-round-two")).await.unwrap().success, Some(true));
        assert!(SourceReadHandler.handle(call(true), context(foreign, "runtime-round-two")).await.is_err());
    }

    #[tokio::test]
    #[cfg(unix)]
    #[ignore = "requires TURA_SCOPED_TEST_RG naming the rg executable to pin"]
    async fn discovery_multi_pattern_rg_executes_literal_or_and_denies_outside_paths() {
        use sha2::{Digest, Sha256};
        let tree = canonical_tempdir();
        let root = tree.path();
        std::fs::create_dir(root.join("src")).unwrap();
        std::fs::write(root.join("src/file"), "compile_local_context\nverify_local_context\nsource_sections\na.b\naxb\nx|y\nx\ny\n--hidden\n").unwrap();
        std::fs::write(root.join("outside"), "OUTSIDE_SECRET\n").unwrap();
        let rg = PathBuf::from(std::env::var_os("TURA_SCOPED_TEST_RG").expect("pinned rg path"))
            .canonicalize().unwrap();
        let cat = PathBuf::from("/bin/cat").canonicalize().unwrap();
        let pin = |path: &Path| json!({"path":path,
            "sha256":format!("{:x}", Sha256::digest(std::fs::read(path).unwrap()))});
        let mut contract = jspace_contract(root);
        contract["read_commands"] = json!({"roots":["src"], "cat":pin(&cat), "rg":pin(&rg)});
        contract["read_scopes"] = json!(["src/**", "outside"]);
        contract["write_scopes"] = json!([]);
        contract["declared_targets"] = json!([]);
        contract["allowed_operations"] = json!(["read", "command"]);
        contract["command_templates"] = json!([]);
        let contract = reseal_jspace(contract);
        let prefix = format!("'{}' --no-config --max-filesize=1M -n -F", rg.to_string_lossy().replace('\'', "'\\''"));
        let service = CommandRunService::new();
        for (id, suffix, expected) in [
            ("three-patterns", "-e 'compile_local_context' -e 'verify_local_context' -e 'source_sections' -- src/file", "1:compile_local_context\n2:verify_local_context\n3:source_sections\n"),
            ("literal-patterns", "-e 'a.b' --regexp 'x|y' -e '--hidden' -- src/file", "4:a.b\n6:x|y\n9:--hidden\n"),
        ] {
            let response = service.execute(json!({"session_id":"rg-patterns", "runtime_id":"rg-worker",
                "session_directory":root, "jspace_contract":contract, "allowed_commands":["bash"],
                "arguments":{"execution_id":id, "commands":[{
                    "command_type":"bash", "command_line":format!("{prefix} {suffix}")}]}})).await.unwrap();
            let result = &response["result"]["results"][0];
            assert_eq!(result["success"], true, "{response}");
            assert_eq!(result["output"]["stdout"], expected, "{response}");
            assert_eq!(result["output"]["terminal_receipt"]["exit_code"], 0, "{response}");
        }
        let denied = service.execute(json!({"session_id":"rg-patterns", "runtime_id":"rg-worker",
            "session_directory":root, "jspace_contract":contract, "allowed_commands":["bash"],
            "arguments":{"execution_id":"outside-patterns", "commands":[{
                "command_type":"bash", "command_line":format!("{prefix} -e OUTSIDE_SECRET -- outside src")}]}})).await.unwrap();
        let result = &denied["result"]["results"][0];
        assert_eq!(result["success"], false, "{denied}");
        assert_eq!(result["jspace_error_code"], "JSPACE_READ_COMMAND_DENIED", "{denied}");
        assert_eq!(result["effect_state"], "not_started", "{denied}");
        assert!(result.get("output").is_none(), "{denied}");
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn dcf_discovery_range_read_executes_pages_and_rejects_stale_or_outside_content() {
        use sha2::{Digest, Sha256};
        let tree = canonical_tempdir();
        let root = tree.path();
        std::fs::create_dir(root.join("src")).unwrap();
        std::fs::write(root.join("src/file"), "first\nsecond\nthird\n").unwrap();
        std::fs::write(root.join("outside"), "outside\n").unwrap();
        let pin = |name: &str| {
            let path = root.join(name);
            std::fs::write(&path, name).unwrap();
            json!({"path":path, "sha256":format!("{:x}", Sha256::digest(name.as_bytes()))})
        };
        let mut contract = jspace_contract(root);
        contract["read_commands"] = json!({"roots":["src"], "cat":pin("cat"), "rg":pin("rg")});
        contract["read_scopes"] = json!(["src/**", "outside"]);
        contract["write_scopes"] = json!([]);
        contract["declared_targets"] = json!([]);
        contract["allowed_operations"] = json!(["read", "command"]);
        contract["command_templates"] = json!([]);
        let contract = reseal_jspace(contract);
        assert!(contract.get("source_read").is_none());
        let service = CommandRunService::new();
        let mut input = json!({"session_id":"dcf-discovery-range", "runtime_id":"range-worker",
            "session_directory":root, "jspace_contract":contract,
            "allowed_commands":["source_read"], "arguments":{"execution_id":"range-first", "commands":[{
                "command_type":"source_read", "command_line":json!(["src/file",1,1]).to_string()}]}});
        let first = service.execute(input.clone()).await.unwrap();
        assert_eq!(first["result"]["results"][0]["success"], true, "{first}");
        let output = &first["result"]["results"][0]["output"];
        assert_eq!(output["stdout"], "1: first\n");
        let digest = output["source_sha256"].as_str().unwrap();
        input["arguments"]["execution_id"] = json!("range-second");
        input["arguments"]["commands"][0]["command_line"] = json!(json!(["src/file",2,3,digest]).to_string());
        let second = service.execute(input.clone()).await.unwrap();
        assert_eq!(second["result"]["results"][0]["success"], true, "{second}");
        assert_eq!(second["result"]["results"][0]["output"]["stdout"], "2: second\n3: third\n");
        std::fs::write(root.join("src/file"), "changed\n").unwrap();
        input["arguments"]["execution_id"] = json!("range-stale");
        let stale = service.execute(input.clone()).await.unwrap();
        assert_ne!(stale["result"]["results"][0]["success"], true, "{stale}");
        assert!(!stale.to_string().contains("1: changed"));
        input["arguments"]["execution_id"] = json!("range-outside");
        input["arguments"]["commands"][0]["command_line"] = json!(json!(["outside",1,1]).to_string());
        let outside = service.execute(input).await.unwrap();
        assert_ne!(outside["result"]["results"][0]["success"], true, "{outside}");
        assert!(outside.to_string().contains("JSPACE_"), "{outside}");
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn observed_digest_router_works_without_verifiers_and_keeps_read_scope() {
        let (tree, _artifacts, mut contract) = admitted_digest_fixture();
        contract["write_scopes"] = json!([]);
        contract["declared_targets"] = json!([]);
        contract.as_object_mut().unwrap().remove("verifier_commands");
        contract.as_object_mut().unwrap().remove("verifier_artifact_root");
        contract = reseal_jspace(contract);
        let service = CommandRunService::new();
        let selector = json!({"path":"src/main.rs", "start_line":1, "end_line":1});
        let mut input = json!({"session_id":"shared-session-label", "runtime_id":"observed-worker-a",
            "session_directory":tree.path(), "jspace_contract":contract,
            "allowed_commands":["source_read"], "arguments":{"execution_id":"observe-first", "commands":[{
                "command_type":"source_read", "command_line":selector.to_string()}]}});
        let first = service.execute(input.clone()).await.unwrap();
        assert_eq!(first["result"]["results"][0]["success"], true, "{first}");
        let mut reference = selector;
        reference["expected_sha256_ref"] = json!("observed");
        input["arguments"]["commands"][0]["command_line"] = json!(reference.to_string());
        input["arguments"]["execution_id"] = json!("observe-reference");
        let second = service.execute(input.clone()).await.unwrap();
        assert_eq!(second["result"]["results"][0]["success"], true, "{second}");
        assert_eq!(second["result"]["results"][0]["output"]["stdout"], "1: before\n");
        assert_eq!(second["result"]["results"][0]["output"]["observed_digest_reference"]["request"], reference);
        input["runtime_id"] = json!("observed-worker-b");
        input["arguments"]["execution_id"] = json!("observe-foreign");
        let foreign = service.execute(input.clone()).await.unwrap();
        assert!(foreign.to_string().contains("SOURCE_READ_DIGEST_REFERENCE_UNAVAILABLE"), "{foreign}");
        input["runtime_id"] = json!("observed-worker-a");
        reference["path"] = json!("src/edit.rs");
        input["arguments"]["execution_id"] = json!("observe-outside-scope");
        input["arguments"]["commands"][0]["command_line"] = json!(reference.to_string());
        let denied = service.execute(input).await.unwrap();
        assert_eq!(denied["result"]["results"][0]["success"], false, "{denied}");
        assert!(denied.to_string().contains("JSPACE_"), "{denied}");
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn admitted_digest_router_preserves_identity_and_rejects_stale_content() {
        let (tree, _artifacts, contract) = admitted_digest_fixture();
        let service = CommandRunService::new();
        let selector = json!({"path":"src/main.rs","start_line":1,"end_line":1,
            "expected_sha256_ref":"admitted"});
        let command = json!({"command_type":"source_read","command_line":selector.to_string()});
        let mut input = json!({"session_id":"admitted-digest-router","session_directory":tree.path(),
            "jspace_contract":contract,"allowed_commands":["source_read"],
            "arguments":{"commands":[command]}});
        let response = service.execute(input.clone()).await.unwrap();
        let result = &response["result"]["results"][0];
        assert_eq!(result["success"], true, "{response}");
        assert_eq!(result["output"]["stdout"], "1: before\n");
        assert_eq!(result["output"]["admitted_digest_reference"]["request"], selector);
        assert!(result["output"].get("source_read_evidence").is_none());
        assert_eq!(result["output"]["terminal_receipt"]["terminal_state"], "completed");
        let receipt_path = result["output"]["terminal_receipt_path"].as_str().unwrap();
        let durable: Value = serde_json::from_slice(&std::fs::read(receipt_path).unwrap()).unwrap();
        assert_eq!(durable, result["output"]["terminal_receipt"]);
        // Cached admission must not bypass comparison with the actual read buffer.
        std::fs::write(tree.path().join("src/main.rs"), "changed\n").unwrap();
        input["arguments"]["execution_id"] = json!("admitted-digest-stale-read");
        let stale = service.execute(input).await.unwrap();
        assert_eq!(stale["result"]["results"][0]["success"], false, "{stale}");
        assert!(stale.to_string().contains("SOURCE_READ_SHA256_MISMATCH"), "{stale}");
        assert_eq!(stale["result"]["results"][0]["output"]["stdout"], "");
    }

    #[tokio::test]
    #[cfg(unix)]
    #[ignore = "requires the hash-bound recorded benchmark source; no provider calls"]
    async fn admitted_digest_recorded_selector_matches_literal_read() {
        use sha2::{Digest, Sha256};
        let source = std::env::var_os("NOKIY_DIGEST_REPLAY_SOURCE").expect("recorded source path");
        let content = std::fs::read(source).unwrap();
        let expected = "c9c59f4e8ff0b06a8bc38756fc0ba3c4021b75308d9e030f7bf501c1de35504d";
        assert_eq!(format!("{:x}", Sha256::digest(&content)), expected);
        let (tree, _artifacts, mut contract) = admitted_digest_fixture();
        let path = "scripts/r19/compare_full1127_guarded_selector.py";
        std::fs::create_dir_all(tree.path().join("scripts/r19")).unwrap();
        let target = tree.path().join(path);
        std::fs::write(&target, &content).unwrap();
        contract["read_scopes"] = json!([path]);
        contract["verifier_commands"][0]["argv"][1] = json!(target);
        contract["verifier_commands"][0]["pinned_files"][0] =
            json!({"path":target,"sha256":expected});
        let contract = reseal_jspace(contract);
        let mut selector = json!({"path":path,"start_line":265,"end_line":295,
            "expected_sha256":"c9c59f4e8ff0b06a8bc38756fc0ba3c4021b75308d9e030f7d9b36baacff"});
        let service = CommandRunService::new();
        let mut outputs = Vec::new();
        for mode in ["recorded-invalid", "literal", "admitted"] {
            if mode == "literal" { selector["expected_sha256"] = json!(expected); }
            if mode == "admitted" {
                selector.as_object_mut().unwrap().remove("expected_sha256");
                selector["expected_sha256_ref"] = json!("admitted");
            }
            let response = service.execute(json!({
                "session_id":"recorded-digest-replay","session_directory":tree.path(),
                "jspace_contract":contract,"allowed_commands":["source_read"],
                "arguments":{"execution_id":mode,"commands":[{
                    "command_type":"source_read","command_line":selector.to_string()
                }]}
            })).await.unwrap();
            let result = &response["result"]["results"][0];
            if mode == "recorded-invalid" {
                assert_eq!(result["success"], false, "{response}");
                assert!(response.to_string().contains("SOURCE_READ_SHA256_INVALID"));
                continue;
            }
            assert_eq!(result["success"], true, "{response}");
            assert_eq!(result["output"]["source_sha256"], expected);
            assert_eq!(result["output"]["start_line"], 265);
            assert_eq!(result["output"]["end_line"], 295);
            assert_eq!(result["output"]["truncated"], false);
            outputs.push(result["output"]["stdout"].clone());
        }
        assert_eq!(outputs.len(), 2);
        assert_eq!(outputs[0], outputs[1]);
        assert_eq!(outputs[0].as_str().unwrap().lines().count(), 31);
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn admitted_digest_router_preflight_rejects_before_any_batch_mutation() {
        let (tree, _artifacts, contract) = admitted_digest_fixture();
        let patch = json!({"command_type":"apply_patch","command_line":
            "*** Begin Patch\n*** Update File: src/edit.rs\n@@\n-before\n+changed\n*** End Patch"});
        for selector in [
            json!({"path":"src/edit.rs","start_line":1,"end_line":1,"expected_sha256_ref":"admitted"}),
            json!({"path":"src/missing.rs","start_line":1,"end_line":1,"expected_sha256_ref":"admitted"}),
            json!({"path":"src/main.rs","start_line":1,"end_line":1,"expected_sha256":"short"}),
            json!({"path":"src/main.rs","start_line":1,"end_line":1,"expected_sha256_ref":"latest"}),
        ] {
            let response = CommandRunService::new().execute(json!({
                "session_id":"admitted-digest-denied","session_directory":tree.path(),
                "jspace_contract":contract,"allowed_commands":["apply_patch","source_read"],
                "arguments":{"commands":[patch,{"command_type":"source_read","command_line":selector.to_string()}]}
            })).await.unwrap();
            assert_eq!(response["result"]["results"][0]["success"], false, "{response}");
            assert_eq!(std::fs::read_to_string(tree.path().join("src/edit.rs")).unwrap(), "before\n");
        }
    }

    #[tokio::test]
    async fn applied_source_delta_obeys_jspace_read_grant_and_receipt() {
        for mode in ["exact", "no_source_read", "no_read", "other_path", "wildcard", "no_command"] {
            let workspace = canonical_tempdir();
            let source = workspace.path().join("src/main.rs");
            std::fs::create_dir(workspace.path().join("src")).unwrap();
            std::fs::write(&source, "old\n").unwrap();
            let mut contract = jspace_contract(workspace.path());
            if mode != "no_source_read" {
                contract["source_read"] = json!(true);
            }
            if mode != "no_source_read" {
                contract["command_templates"] = json!([]);
            }
            contract["read_scopes"] = match mode {
                "other_path" => json!(["src/other.rs"]),
                "wildcard" => json!(["src/**", "src/other.rs"]),
                _ => json!(["src/main.rs"]),
            };
            if mode == "no_read" { contract["allowed_operations"] = json!(["modify","command"]); }
            if mode == "no_command" { contract["allowed_operations"] = json!(["read", "modify"]); }
            let service = CommandRunService::new();
            let response = service.execute(json!({
                "session_id":format!("patch-delta-{mode}"), "runtime_id":"delta-test",
                "session_directory":workspace.path(), "jspace_contract":reseal_jspace(contract),
                "arguments":{"commands":[{"command":"apply_patch", "command_line":
                    "*** Begin Patch\n*** Update File: src/main.rs\n@@\n-old\n+new\n*** End Patch"}]}
            })).await;
            if matches!(mode, "no_read" | "no_command") {
                let error = response.unwrap_err().to_string();
                assert!(error.contains("JSPACE_SOURCE_READ_DENIED"), "{mode}: {error}");
                assert_eq!(std::fs::read_to_string(&source).unwrap(), "old\n");
                continue;
            }
            let response = response.unwrap();
            let result = &response["result"]["results"][0];
            assert_eq!(result["success"], true, "{mode}: {response}");
            let output = &result["output"];
            assert!(output["terminal_receipt"].is_object(), "{response}");
            assert_eq!(output["changes"][0].get("source_delta").is_some(), mode == "exact", "{mode}: {response}");
            assert_eq!(std::fs::read_to_string(source).unwrap(), "new\n");
        }
    }

    #[cfg(unix)]
    fn mapped_jspace_fixture() -> (tempfile::TempDir, PathBuf, PathBuf, Value) {
        use sha2::{Digest, Sha256};
        use std::os::unix::fs::symlink;
        use tura_path::mapped_source_read::MappedSourceReadBinding;
        let tree = canonical_tempdir();
        let repo = tree.path().join("repo");
        let physical = tree.path().join("external/dcf/v2");
        std::fs::create_dir_all(repo.join("config/contracts")).expect("contract directory");
        std::fs::create_dir_all(repo.join("output")).expect("logical parent");
        std::fs::create_dir_all(repo.join("src")).expect("source directory");
        std::fs::create_dir_all(&physical).expect("physical root");
        std::fs::write(repo.join("src/main.rs"), "before\n").expect("patch target");
        std::fs::write(physical.join("current.json"), "{\"generation\":1}\n").expect("pointer");
        std::fs::write(physical.join("other.json"), "not admitted\n").expect("other pointer");
        symlink(physical.parent().unwrap(), repo.join("output/dcf_runtime")).expect("alias");
        let logical = "output/dcf_runtime/v2/current.json";
        let bytes = serde_json::to_vec(&json!({
            "schema_version":"dcf_v2_contract_v1", "status":"active",
            "authority_effect":"none", "no_apply":true,
            "runtime":{"namespace":"output/dcf_runtime/v2", "current_pointer":logical},
            "two_tier_storage_cutover":{
                "logical_runtime_namespace":"output/dcf_runtime/v2",
                "target_physical_runtime_root":physical,
            }
        })).expect("DCF contract");
        std::fs::write(repo.join("config/contracts/dcf_v2_contract.json"), &bytes).expect("contract");
        let binding = MappedSourceReadBinding::derive(
            &repo, &bytes, &format!("{:x}", Sha256::digest(&bytes)), logical,
        ).expect("binding");
        let mut contract = jspace_contract(&repo);
        contract["read_scopes"] = json!([logical]);
        contract["write_scopes"] = json!(["src/main.rs", logical]);
        contract["declared_targets"] = json!(["src/main.rs", logical]);
        contract["command_templates"] = json!([]);
        contract["source_read"] = json!(true);
        contract["mapped_source_reads"] = json!([binding]);
        (tree, repo, physical, reseal_jspace(contract))
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn mapped_source_read_router_bridge_preserves_logical_output_and_terminal_receipt() {
        let (_tree, repo, physical, contract) = mapped_jspace_fixture();
        let logical = "output/dcf_runtime/v2/current.json";
        let response = CommandRunService::new().execute(json!({
            "session_id":"mapped-router", "runtime_id":"mapped-test",
            "session_directory":repo, "jspace_contract":contract,
            "allowed_commands":["source_read"],
            "arguments":{"commands":[{
                "command_type":"source_read",
                "command_line":json!({"path":logical,"start_line":1,"end_line":1}).to_string()
            }]}
        })).await.expect("mapped router response");
        let result = &response["result"]["results"][0];
        assert_eq!(result["success"], true, "{response}");
        assert_eq!(result["output"]["stdout"], "1: {\"generation\":1}\n");
        assert_eq!(result["output"]["path"], logical);
        assert_eq!(result["output"]["terminal_receipt"]["terminal_state"], "completed");
        assert_eq!(result["output"]["terminal_receipt"]["authority_effect"], "none");
        let receipt = result["output"]["terminal_receipt_path"].as_str().expect("receipt path");
        let durable: Value = serde_json::from_slice(&std::fs::read(receipt).expect("receipt bytes")).expect("receipt JSON");
        assert_eq!(durable, result["output"]["terminal_receipt"]);
        assert_eq!(std::fs::read_to_string(physical.join("current.json")).unwrap(), "{\"generation\":1}\n");
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn mapped_source_read_router_keeps_full_batch_preflight_and_workdir_denials() {
        let (_tree, repo, physical, contract) = mapped_jspace_fixture();
        let logical = "output/dcf_runtime/v2/current.json";
        let patch = json!({"command_type":"apply_patch", "command_line":
            "*** Begin Patch\n*** Update File: src/main.rs\n@@\n-before\n+changed\n*** End Patch"});
        let physical_target = physical.join("current.json").to_string_lossy().into_owned();
        for (scenario, command) in [
            ("workdir", json!({"command_type":"source_read", "workdir":"src", "command_line":
                json!({"path":logical,"start_line":1,"end_line":1}).to_string()})),
            ("sibling", json!({"command_type":"source_read", "command_line":
                json!({"path":"output/dcf_runtime/v2/other.json","start_line":1,"end_line":1}).to_string()})),
            ("physical", json!({"command_type":"source_read", "command_line":
                json!({"path":physical_target,"start_line":1,"end_line":1}).to_string()})),
            ("alias", json!({"command_type":"source_read", "command_line":
                json!({"path":"output/./dcf_runtime/v2/current.json","start_line":1,"end_line":1}).to_string()})),
            ("cat", json!({"command_type":"bash", "command_line":"cat output/dcf_runtime/v2/current.json"})),
            ("write", json!({"command_type":"apply_patch", "command_line":
                "*** Begin Patch\n*** Update File: output/dcf_runtime/v2/current.json\n@@\n-{\"generation\":1}\n+changed\n*** End Patch"})),
        ] {
            let response = CommandRunService::new().execute(json!({
                "session_id":format!("mapped-deny-{scenario}"), "session_directory":repo,
                "jspace_contract":contract, "allowed_commands":["apply_patch","source_read","bash"],
                "arguments":{"commands":[patch,command]}
            })).await.expect("router denial");
            assert_eq!(response["result"]["results"][0]["success"], false, "{scenario}: {response}");
            assert_eq!(std::fs::read_to_string(repo.join("src/main.rs")).unwrap(), "before\n", "{scenario}");
            assert_eq!(std::fs::read_to_string(physical.join("current.json")).unwrap(), "{\"generation\":1}\n", "{scenario}");
        }
    }

    #[test]
    #[cfg(unix)]
    fn mapped_source_read_router_does_not_use_unadmitted_arguments_or_stale_bindings() {
        let (_tree, repo, _physical, contract) = mapped_jspace_fixture();
        let arguments = json!({"commands":[{"command_type":"source_read", "command_line":
            json!({"path":"output/dcf_runtime/v2/current.json","start_line":1,"end_line":1}).to_string()}],
            "mapped_source_reads":contract["mapped_source_reads"]});
        let matcher = tura_path::jspace::JSpaceMatcher::from_value(&repo, &contract).expect("admission");
        validate_jspace_arguments(&matcher, &arguments).expect("mapped preflight");
        let mut ordinary = contract.clone();
        ordinary.as_object_mut().unwrap().remove("mapped_source_reads");
        let ordinary = tura_path::jspace::JSpaceMatcher::from_value(&repo, &reseal_jspace(ordinary)).expect("ordinary admission");
        assert!(validate_jspace_arguments(&ordinary, &arguments).is_err());
        std::fs::write(repo.join("config/contracts/dcf_v2_contract.json"), "{}\n").expect("contract change");
        assert!(validate_jspace_arguments(&matcher, &arguments).is_err());
    }

    #[test]
    fn prepared_source_read_contract_passes_router_preflight_without_execution() {
        let Some(workspace) = std::env::var_os("TURA_SCOPED_TEST_WORKSPACE") else {
            return;
        };
        let contract_path = std::env::var_os("TURA_SCOPED_TEST_JSPACE")
            .expect("prepared J-Space path is required with the workspace");
        let workspace = std::path::PathBuf::from(workspace);
        let contract: Value =
            serde_json::from_slice(&std::fs::read(contract_path).expect("prepared J-Space JSON"))
                .expect("prepared J-Space object");
        let matcher = tura_path::jspace::JSpaceMatcher::from_value(&workspace, &contract)
            .expect("prepared contract admission");
        validate_jspace_arguments(
            &matcher,
            &json!({"commands": [{
                "command_type": "source_read",
                "command_line": json!({
                    "path": "TASK.md", "start_line": 1, "end_line": 1
                }).to_string()
            }]}),
        )
        .expect("exact source_read router preflight");
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn malformed_source_read_has_durable_typed_non_execution_evidence() {
        use code_tools::commands::source_read::SourceReadPreExecutionRejection;
        let workspace = canonical_tempdir();
        std::fs::create_dir(workspace.path().join("src")).expect("source directory");
        std::fs::write(workspace.path().join("src/main.rs"), "answer\n").expect("source");
        let mut contract = jspace_contract(workspace.path());
        contract["read_scopes"] = json!(["src/main.rs"]);
        contract["source_read"] = json!(true);
        let contract = reseal_jspace(contract);
        let service = CommandRunService::new();
        for (index, line) in [
            r#"{"path":"src/main.rs","start_line":1,"end_line":ninety}"#,
            r#"{"path":"src/main.rs","start_line":1,"end_line":"ninety"}"#,
            r#"{"path":"src/main.rs","start_line":1,"end_line":1,"expected_sha256":"5c7d6faa308bb6125375d6b1b06a0106d5e09fd22b182d51a6bf1c6af1c6af1c6af5321124"}"#,
        ].into_iter().enumerate() {
            let command = json!({"command_type":"source_read", "command":"source_read", "command_line":line, "step":1});
            let response = service.execute_with_request_id(json!({
                "session_id":"rejected-session", "runtime_id":"rejected-runtime",
                "session_directory":workspace.path(), "jspace_contract":contract,
                "allowed_commands":["source_read"], "arguments":{"commands":[command]},
            }), Some(&format!("rejected-read-{index}"))).await.expect("preflight response");
            let result = &response["result"]["results"][0];
            assert_eq!(result["success"], false);
            assert_eq!(result["command_type"], "source_read");
            assert_eq!(result["jspace_error_code"], "JSPACE_SOURCE_READ_INVALID");
            let evidence = &result["output"]["pre_execution_rejection"];
            assert_eq!(evidence["rejection_kind"],
                if index == 2 { "invalid_expected_sha256" } else { "json_syntax_or_shape" });
            assert_eq!(evidence["effect_state"], "not_started");
            assert_eq!(evidence["process_started"], false);
            assert_eq!(evidence["source_content_read"], false);
            assert_eq!(evidence["mutation_count"], 0);
            assert!(result["output"].get("stdout").is_none());
            assert!(result["output"].get("terminal_receipt").is_none());
            let proof = SourceReadPreExecutionRejection::parse_bounded(evidence).expect("typed proof");
            let store = super::ReceiptStore::open_existing(workspace.path()).expect("durable store");
            assert!(proof.authenticate(&store, "rejected-session", "rejected-runtime",
                contract["authorization_semantic_sha256"].as_str().unwrap(), &command, result));
            let names = store.list_names().expect("audit entries");
            assert_eq!(names.len(), index + 1, "no batch admission, command claim, or execution receipt");
            assert!(names.contains(&proof.receipt_name()));
            assert_eq!(service.active_count(), 0);
        }
        let corrected = service.execute_with_request_id(json!({
            "session_id":"rejected-session", "runtime_id":"rejected-runtime",
            "session_directory":workspace.path(), "jspace_contract":contract,
            "allowed_commands":["source_read"], "arguments":{"commands":[{
                "command_type":"source_read", "command_line":r#"{"path":"src/main.rs","start_line":1,"end_line":1}"#,
                "step":1,
            }]},
        }), Some("corrected-read")).await.expect("corrected read");
        let corrected = &corrected["result"]["results"][0];
        assert_eq!(corrected["success"], true);
        assert_eq!(corrected["output"]["stdout"], "1: answer\n");
        assert!(corrected["output"].get("pre_execution_rejection").is_none());
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn policy_path_stale_and_mixed_failures_never_get_recovery_evidence() {
        // (existing scenarios below)
        policy_path_scenarios().await;
    }

    // Real tasks 2026-10-23..25: inspect workers' apply_patch, rg and payload refusals ended
    // "blocked" because nothing durable showed the refused command never ran.
    #[tokio::test]
    #[cfg(unix)]
    async fn jspace_policy_denial_leaves_a_durable_non_execution_witness() {
        use code_tools::command_run::JSpacePolicyDenial;
        let workspace = canonical_tempdir();
        std::fs::create_dir(workspace.path().join("src")).expect("source directory");
        std::fs::write(workspace.path().join("src/main.rs"), "answer\n").expect("source");
        let contract = reseal_jspace(jspace_contract(workspace.path()));
        let authorization = contract["authorization_semantic_sha256"].as_str().unwrap().to_owned();
        let command = json!({"command_type": "apply_patch", "step": 1,
            "command_line": "*** Begin Patch\n*** Delete File: src/main.rs\n*** End Patch\n"});
        let response = CommandRunService::new().execute_with_request_id(json!({
            "session_id": "denied-session", "runtime_id": "denied-runtime",
            "session_directory": workspace.path(), "jspace_contract": contract,
            "arguments": {"commands": [command.clone()]},
        }), Some("denied-call")).await.expect("denial response");
        let mut result = response["result"]["results"][0].clone();
        assert_eq!(result["command_type"], "jspace", "{result}");
        assert_eq!(result["delivery_state"], "deterministic_policy_denial");
        assert!(result["jspace_error_code"].as_str().unwrap().starts_with("JSPACE_"), "{result}");
        assert_eq!(std::fs::read_to_string(workspace.path().join("src/main.rs")).unwrap(), "answer\n");
        result["step"] = json!(1); // the runtime reports the command's step with its result
        let (_, calls) = code_tools::command_run::command_run_batch_identity(
            &json!({"execution_id": "denied-call", "commands": [command.clone()]})).expect("call identity");
        let store = super::ReceiptStore::open_existing(workspace.path()).expect("durable store");
        assert_eq!(store.list_names().expect("audit entries").len(), 1, "a witness only, no admission or claim");
        assert!(JSpacePolicyDenial::authenticate(&store, "denied-session", "denied-runtime", &calls[0],
            Some(&authorization), &command, &result));
        let mut other_line = command.clone();
        other_line["command_line"] = json!("*** Begin Patch\n*** Delete File: src/other.rs\n*** End Patch\n");
        let mut other_error = result.clone();
        other_error["error"] = json!("JSPACE_OPERATION_DENIED: something else");
        for (call, command, result, authorization) in [
            ("other-call", &command, &result, authorization.as_str()),
            (calls[0].as_str(), &other_line, &result, authorization.as_str()),
            (calls[0].as_str(), &command, &other_error, authorization.as_str()),
            (calls[0].as_str(), &command, &result, "0000000000000000000000000000000000000000000000000000000000000000"),
        ] {
            assert!(!JSpacePolicyDenial::authenticate(&store, "denied-session", "denied-runtime", call,
                Some(authorization), command, result));
        }
        assert!(!JSpacePolicyDenial::authenticate(&store, "other-session", "denied-runtime", &calls[0],
            Some(&authorization), &command, &result));
    }

    async fn policy_path_scenarios() {
        for scenario in 0..12 {
            let workspace = canonical_tempdir();
            std::fs::create_dir(workspace.path().join("src")).expect("source directory");
            std::fs::write(workspace.path().join("src/main.rs"), "answer\n").expect("source");
            std::fs::write(workspace.path().join("src/other.rs"), "outside\n").expect("outside");
            let mut contract = jspace_contract(workspace.path());
            contract["read_scopes"] = json!(["src/main.rs"]);
            if scenario != 0 { contract["source_read"] = json!(true); }
            let mut allowed = json!(["source_read"]);
            let mut command = json!({"command_type":"source_read", "step":1,
                "command_line":r#"{"path":"src/main.rs","start_line":1,"end_line":ninety}"#});
            match scenario {
                1 => allowed = json!(["task_status"]),
                2 => command["workdir"] = json!(workspace.path()),
                3 => command["command_line"] = json!(r#"{"path":"../secret","start_line":1,"end_line":1}"#),
                4 => command["command_line"] = json!(r#"{"path":"src/other.rs","start_line":1,"end_line":1}"#),
                5 => command["command_line"] = json!(r#"{"path":"src/missing.rs","start_line":1,"end_line":1}"#),
                6 => command["command_line"] = json!(json!({"path":"src/main.rs", "start_line":1,
                    "end_line":1, "expected_sha256":"0".repeat(64)}).to_string()),
                8 => command["command_type"] = json!("unknown_command"),
                9 => {
                    std::os::unix::fs::symlink("main.rs", workspace.path().join("src/link.rs")).expect("symlink");
                    command["command_line"] = json!(r#"{"path":"src/link.rs","start_line":1,"end_line":1}"#);
                }
                10 => command["command_line"] = json!(r#"{"path":"../secret","start_line":1,"end_line":1,"expected_sha256":"bad"}"#),
                11 => {
                    contract.as_object_mut().expect("contract").remove("source_read");
                    command["command_line"] = json!(r#"{"path":"src/main.rs","start_line":1,"end_line":1,"expected_sha256":"bad"}"#);
                }
                _ => {}
            }
            let mut commands = vec![command];
            if scenario == 7 {
                commands.push(json!({"command_type":"apply_patch", "step":2, "command_line":"fixture patch"}));
            }
            let response = CommandRunService::new().execute_with_request_id(json!({
                "session_id":"denied-session", "runtime_id":"denied-runtime", "session_directory":workspace.path(),
                "jspace_contract":reseal_jspace(contract), "allowed_commands":allowed,
                "arguments":{"commands":commands},
            }), Some("denied-read")).await.expect("denied response");
            assert!(response["result"]["results"].as_array().unwrap().iter().all(|result| result["success"] == false));
            assert!(!response.to_string().contains("nokiy_source_read_pre_execution_rejection_v1"), "scenario {scenario}");
        }
    }

    #[cfg(unix)]
    async fn check_missing_target_paths(schema: &str) {
        let workspace = canonical_tempdir();
        std::fs::create_dir(workspace.path().join("src")).unwrap();
        std::fs::write(workspace.path().join("src/other.rs"), "outside\n").unwrap();
        std::os::unix::fs::symlink("other.rs", workspace.path().join("src/link.rs")).unwrap();
        let mut contract = jspace_contract(workspace.path());
        contract["read_scopes"] = json!(["src/missing.rs", "src/link.rs"]);
        contract["source_read"] = json!(true);
        contract["command_templates"] = json!([]);
        if schema == "jspace_contract_v3" {
            contract["schema_version"] = json!(schema);
            contract["allowed_operations"] = json!(["command"]);
            contract["effect_capabilities"] = json!([{
                "domain_id":"source", "root":workspace.path(),
                "read_scopes":["src/missing.rs", "src/link.rs"], "write_scopes":[],
                "allowed_operations":["read"], "declared_targets":[]
            }]);
            for field in ["read_scopes", "write_scopes", "declared_targets"] {
                contract.as_object_mut().unwrap().remove(field);
            }
        }
        let contract = reseal_jspace(contract);
        let service = CommandRunService::new();
        let read = |path: &str| json!({
            "session_id":"missing-source-session", "runtime_id":"missing-source-runtime",
            "session_directory":workspace.path(), "jspace_contract":contract,
            "allowed_commands":["source_read"], "arguments":{"commands":[{
                "command_type":"source_read", "command_line":json!({
                    "path":path, "start_line":1, "end_line":1}).to_string()}]}
        });
        let missing = service.execute(read("src/missing.rs")).await.unwrap();
        let result = &missing["result"]["results"][0];
        assert_eq!(result["success"], false, "{missing}");
        assert!(result.get("jspace_error_code").is_none(), "{missing}");
        assert_eq!(result["output"]["error_type"], "SOURCE_READ_TARGET_NOT_FOUND", "{missing}");
        assert_eq!(result["output"]["terminal_receipt"]["termination_proven"], true);
        assert_eq!(result["output"]["terminal_receipt"]["authority_effect"], "none");
        for path in ["src/other.rs", "src/outside-missing.rs"] {
            let denied = service.execute(read(path)).await.unwrap();
            assert_eq!(denied["result"]["results"][0]["jspace_error_code"],
                "JSPACE_EXPANSION_REQUIRED", "{denied}");
        }
        let symlink = service.execute(read("src/link.rs")).await.unwrap();
        assert_eq!(symlink["result"]["results"][0]["jspace_error_code"], "JSPACE_SOURCE_READ_PATH_DENIED");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn source_read_missing_target_keeps_scope_and_symlink_guards() {
        for schema in ["jspace_contract_v2", "jspace_contract_v3"] {
            check_missing_target_paths(schema).await;
        }
    }

    #[tokio::test]
    async fn source_read_requires_exact_jspace_scope_and_keeps_a_terminal_receipt() {
        let workspace = canonical_tempdir();
        std::fs::create_dir(workspace.path().join("src")).expect("source directory");
        std::fs::write(workspace.path().join("src/main.rs"), "alpha\nbeta\n").expect("source file");
        std::fs::write(workspace.path().join("src/other.rs"), "outside\n").expect("other file");
        let mut contract = jspace_contract(workspace.path());
        contract["read_scopes"] = json!(["src/main.rs"]);
        contract["command_templates"] = json!([]);
        contract["source_read"] = json!(true);
        let contract = reseal_jspace(contract);
        let service = CommandRunService::new();
        let read = |path: &str| {
            json!({
                "session_id": "source-read-session",
                "runtime_id": "source-read-runtime",
                "session_directory": workspace.path(),
                "jspace_contract": contract,
                "arguments": {"commands": [{
                    "command_type": "source_read",
                    "command_line": json!({"path": path, "start_line": 1, "end_line": 2}).to_string()
                }]},
                "allowed_commands": ["source_read"]
            })
        };
        let accepted = service
            .execute(read("src/main.rs"))
            .await
            .expect("accepted read");
        let result = &accepted["result"]["results"][0];
        assert_eq!(result["success"], true, "{accepted}");
        assert_eq!(result["output"]["stdout"], "1: alpha\n2: beta\n");
        assert_eq!(
            result["output"]["terminal_receipt"]["authority_effect"],
            "none"
        );
        assert!(
            serde_json::to_vec(&result["output"])
                .expect("result JSON")
                .len()
                <= 8192
        );

        let denied = service
            .execute(read("src/other.rs"))
            .await
            .expect("denied read");
        assert_eq!(
            denied["result"]["results"][0]["jspace_error_code"],
            "JSPACE_EXPANSION_REQUIRED"
        );
        let shell = service.execute(json!({
            "session_id": "source-read-session",
            "runtime_id": "source-read-runtime",
            "session_directory": workspace.path(),
            "jspace_contract": contract,
            "arguments": {"commands": [{"command_type": "bash", "command_line": "cat src/main.rs"}]},
            "allowed_commands": ["bash"]
        })).await.expect("shell denial");
        assert_eq!(
            shell["result"]["results"][0]["jspace_error_code"],
            "JSPACE_COMMAND_DENIED"
        );
    }

    #[tokio::test]
    async fn source_read_without_jspace_is_deterministically_denied() {
        let workspace = canonical_tempdir();
        let response = CommandRunService::new().execute(json!({
            "session_id": "no-jspace-source-read",
            "session_directory": workspace.path(),
            "arguments": {"commands": [{
                "command_type": "source_read",
                "command_line": json!({"path": "src/main.rs", "start_line": 1, "end_line": 1}).to_string()
            }]},
            "allowed_commands": ["source_read"]
        })).await.expect("denial response");
        assert_eq!(
            response["result"]["results"][0]["jspace_error_code"],
            "JSPACE_SOURCE_READ_DENIED"
        );
        assert_eq!(
            response["result"]["results"][0]["effect_state"],
            "not_started"
        );
    }

    #[tokio::test]
    async fn source_read_cannot_hide_behind_another_command_field() {
        let workspace = canonical_tempdir();
        std::fs::create_dir(workspace.path().join("src")).expect("source directory");
        std::fs::write(workspace.path().join("src/main.rs"), "allowed\n").expect("allowed file");
        std::fs::write(workspace.path().join("src/other.rs"), "outside\n").expect("outside file");
        let hidden_read = json!({"commands": [{
            "command_type": "source_read",
            "command": "task_status",
            "command_line": json!({
                "path": "src/other.rs", "start_line": 1, "end_line": 1
            }).to_string()
        }]});
        let service = CommandRunService::new();
        let no_jspace = service
            .execute(json!({
                "session_id": "hidden-source-read-no-jspace",
                "session_directory": workspace.path(),
                "arguments": hidden_read,
                "allowed_commands": ["source_read"]
            }))
            .await
            .expect("no-JSpace denial");
        assert_eq!(
            no_jspace["result"]["results"][0]["jspace_error_code"],
            "JSPACE_SOURCE_READ_DENIED"
        );

        let mut exact = jspace_contract(workspace.path());
        exact["read_scopes"] = json!(["src/main.rs"]);
        exact["source_read"] = json!(true);
        let out_of_scope = service
            .execute(json!({
                "session_id": "hidden-source-read-out-of-scope",
                "session_directory": workspace.path(),
                "jspace_contract": reseal_jspace(exact),
                "arguments": hidden_read,
                "allowed_commands": ["source_read"]
            }))
            .await
            .expect("out-of-scope denial");
        assert_eq!(
            out_of_scope["result"]["results"][0]["jspace_error_code"],
            "JSPACE_EXPANSION_REQUIRED"
        );
    }

    #[tokio::test]
    async fn source_read_grant_keeps_patch_limited_to_its_declared_write_target() {
        let workspace = canonical_tempdir();
        std::fs::create_dir(workspace.path().join("src")).expect("source directory");
        let target = workspace.path().join("src/main.rs");
        std::fs::write(&target, "before\n").expect("source file");
        let mut contract = jspace_contract(workspace.path());
        contract["read_scopes"] = json!(["src/main.rs"]);
        contract["write_scopes"] = json!(["src/main.rs"]);
        contract["command_templates"] = json!([]);
        contract["source_read"] = json!(true);
        let contract = reseal_jspace(contract);
        let service = CommandRunService::new();
        let patch = |command_line: &str| {
            json!({
                "session_id": "source-read-with-patch",
                "session_directory": workspace.path(),
                "jspace_contract": contract,
                "arguments": {"commands": [{
                    "command_type": "apply_patch", "command_line": command_line
                }]},
                "allowed_commands": ["apply_patch"]
            })
        };
        let accepted = service
            .execute(patch(
                "*** Begin Patch\n*** Update File: src/main.rs\n@@\n-before\n+after\n*** End Patch",
            ))
            .await
            .expect("declared patch");
        assert_eq!(
            accepted["result"]["results"][0]["success"], true,
            "{accepted}"
        );
        assert_eq!(
            std::fs::read_to_string(&target).expect("patched source"),
            "after\n"
        );

        let denied = service
            .execute(patch(
                "*** Begin Patch\n*** Add File: src/other.rs\n+outside\n*** End Patch",
            ))
            .await
            .expect("out-of-scope patch denial");
        assert_eq!(
            denied["result"]["results"][0]["jspace_error_code"],
            "JSPACE_EXPANSION_REQUIRED"
        );
        assert!(!workspace.path().join("src/other.rs").exists());
    }

    #[tokio::test]
    async fn command_run_service_executes_inside_requested_workspace() {
        let workspace = canonical_tempdir();
        let command_line = json!({
            "status": "done",
            "task_group": "订单清结算微服务"
        })
        .to_string();
        let response = CommandRunService::new()
            .execute(json!({
                "session_id": "session-1",
                "runtime_id": "runtime-1",
                "session_directory": workspace.path().display().to_string(),
                "arguments": {
                    "commands": [{
                        "command": "task_status",
                        "command_line": command_line
                    }]
                },
                "allowed_commands": ["task_status"]
            }))
            .await
            .expect("router command_run should execute");

        assert_eq!(response["owner"], "router");
        assert_eq!(response["session_id"], "session-1");
        assert_eq!(response["runtime_id"], "runtime-1");
        assert_eq!(
            response["result"]["results"][0]["command_type"],
            "task_status"
        );
        assert_eq!(response["result"]["results"][0]["success"], true);
        assert_eq!(CommandRunService::new().active_count(), 0);
    }

    #[tokio::test]
    async fn command_run_service_tracks_active_requests() {
        let workspace = canonical_tempdir();
        let service = CommandRunService::new();
        assert_eq!(service.active_count(), 0);

        let request = json!({
            "session_id": "session-active",
            "runtime_id": "runtime-active",
            "session_directory": workspace.path().display().to_string(),
            "arguments": {
                "commands": [{
                    "command": "shell_command",
                    "command_line": json!({
                        "command": delayed_read_only_command("active", ACTIVE_FIXTURE_DELAY_MS),
                        "timeout_ms": READ_ONLY_FIXTURE_TIMEOUT_MS
                    }).to_string()
                }]
            }
        });
        let running = {
            let service = service.clone();
            tokio::spawn(async move { service.execute(request).await })
        };

        let started = Instant::now();
        while service.active_count() == 0 && started.elapsed().as_secs() < 2 {
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        assert_eq!(service.active_count(), 1);
        running
            .await
            .expect("command_run task should join")
            .expect("command_run should finish");
        assert_eq!(service.active_count(), 0);
    }

    #[tokio::test]
    async fn command_run_service_cancels_only_the_exact_session() {
        let workspace = canonical_tempdir();
        let service = CommandRunService::new();
        let request = |session_id: &str, label: &str| {
            json!({
                "session_id": session_id,
                "runtime_id": format!("runtime-{session_id}"),
                "session_directory": workspace.path().display().to_string(),
                "arguments": {
                    "commands": [{
                        "command": "shell_command",
                        "command_line": json!({
                            "command": delayed_read_only_command(label, 5_000),
                            "timeout_ms": READ_ONLY_FIXTURE_TIMEOUT_MS
                        }).to_string()
                    }]
                }
            })
        };
        let first = {
            let service = service.clone();
            let request = request("cancel-first", "first");
            tokio::spawn(async move {
                service
                    .execute_with_request_id(request, Some("cancel-first-execution"))
                    .await
            })
        };
        let second = {
            let service = service.clone();
            let request = request("keep-second", "second");
            tokio::spawn(async move {
                service
                    .execute_with_request_id(request, Some("keep-second-execution"))
                    .await
            })
        };

        let started = Instant::now();
        while (service.active_count_for_session("cancel-first") == 0
            || service.active_count_for_session("keep-second") == 0)
            && started.elapsed() < Duration::from_secs(2)
        {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert_eq!(service.cancel_session("cancel-first"), 1);
        tokio::time::timeout(
            Duration::from_secs(2),
            service.wait_for_session_idle("cancel-first"),
        )
        .await
        .expect("cancelled session should drain promptly");
        assert_eq!(service.active_count_for_session("cancel-first"), 0);
        assert_eq!(service.active_count_for_session("keep-second"), 1);

        assert_eq!(service.cancel_session("keep-second"), 1);
        first
            .await
            .expect("first command task should join")
            .expect("first command response should remain deterministic");
        second
            .await
            .expect("second command task should join")
            .expect("second command response should remain deterministic");
        assert_eq!(service.active_count(), 0);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn outer_abort_preserves_router_ownership_until_seven_command_batch_terminalizes() {
        let workspace = canonical_tempdir();
        let receipt_directory = workspace.path().join(".tura/run/command_receipts");
        let first_gate = workspace.path().join("first-step.fifo");
        let cancellation_gate = workspace.path().join("cancel-step.fifo");
        for gate in [&first_gate, &cancellation_gate] {
            let status = std::process::Command::new("mkfifo")
                .arg(gate)
                .status()
                .expect("create FIFO");
            assert!(status.success(), "mkfifo failed for {}", gate.display());
        }
        let shell_item = |command: String, step: u64| {
            json!({
                "command": "shell_command",
                "command_line": json!({
                    "command": command,
                    "timeout_ms": 10_000
                }).to_string(),
                "step": step
            })
        };
        let mut commands = (0..4)
            .map(|index| shell_item(format!("printf 'ok-{index}\\n'"), 1))
            .collect::<Vec<_>>();
        commands.push(shell_item(format!("cat {}", first_gate.display()), 1));
        commands.push(shell_item("/bin/sh -c 'exit 2'".to_string(), 2));
        commands.push(shell_item(
            format!("cat {}", cancellation_gate.display()),
            3,
        ));

        let service = CommandRunService::new();
        let request = json!({
            "session_id": "outer-abort-session",
            "runtime_id": "outer-abort-runtime",
            "session_directory": workspace.path(),
            "arguments": {"commands": commands}
        });
        let outer = {
            let service = service.clone();
            tokio::spawn(async move {
                service
                    .execute_with_request_id(request, Some("outer-abort-seven"))
                    .await
            })
        };

        wait_for_command_record_count(&receipt_directory, true, 5).await;
        assert!(!command_record_path(&receipt_directory, "outer-abort-seven", 5, true).exists());
        assert!(!command_record_path(&receipt_directory, "outer-abort-seven", 6, true).exists());
        outer.abort();
        assert!(
            outer
                .await
                .expect_err("outer request must be aborted")
                .is_cancelled()
        );
        assert_eq!(service.active_count_for_session("outer-abort-session"), 1);
        assert!(
            tokio::time::timeout(
                Duration::from_millis(100),
                service.wait_for_session_idle("outer-abort-session")
            )
            .await
            .is_err(),
            "idle must remain false while Router-owned work is active"
        );

        let release = std::thread::spawn({
            let first_gate = first_gate.clone();
            move || std::fs::write(first_gate, b"released\n").expect("release first step")
        });
        release.join().expect("FIFO writer");
        wait_for_command_record_count(&receipt_directory, true, 7).await;
        assert_eq!(service.cancel_session("outer-abort-session"), 1);
        tokio::time::timeout(
            Duration::from_secs(5),
            service.wait_for_session_idle("outer-abort-session"),
        )
        .await
        .expect("cancelled supervised batch must terminalize");
        wait_for_command_record_count(&receipt_directory, false, 7).await;
        assert_eq!(service.active_count_for_session("outer-abort-session"), 0);

        for index in 0..7 {
            let claim = read_command_record(&receipt_directory, "outer-abort-seven", index, true);
            let receipt =
                read_command_record(&receipt_directory, "outer-abort-seven", index, false);
            assert_eq!(claim["execution_count"], 1, "{claim}");
            assert_eq!(
                claim["state"], receipt["terminal_state"],
                "{claim} {receipt}"
            );
            match index {
                0..=4 => {
                    assert_eq!(receipt["outcome"], "known", "{receipt}");
                    assert_eq!(receipt["terminal_state"], "completed", "{receipt}");
                    assert_eq!(receipt["exit_code"], 0, "{receipt}");
                }
                5 => {
                    assert_eq!(receipt["outcome"], "known", "{receipt}");
                    assert_eq!(receipt["terminal_state"], "failed", "{receipt}");
                    assert_eq!(receipt["exit_code"], 2, "{receipt}");
                }
                6 => {
                    assert!(
                        matches!(
                            receipt["terminal_state"].as_str(),
                            Some("cancelled" | "terminated" | "not_started")
                        ),
                        "{receipt}"
                    );
                    assert_eq!(receipt["outcome"], "unknown", "{receipt}");
                    assert_eq!(receipt["reconcile_required"], true, "{receipt}");
                }
                _ => unreachable!(),
            }
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn worker_panic_reaps_claimed_process_before_releasing_router_guard() {
        let workspace = canonical_tempdir();
        let receipt_directory = workspace.path().join(".tura/run/command_receipts");
        let late_effect = workspace.path().join("late-effect.txt");
        let service = CommandRunService::new();
        let cleanup_gate = service.install_panic_cleanup_gate();
        let request = json!({
            "session_id": "panic-cleanup-session",
            "runtime_id": "panic-cleanup-runtime",
            "session_directory": workspace.path(),
            "command_env": {super::TEST_PANIC_AFTER_RUNNING_CLAIM: "1"},
            "arguments": {
                "commands": [{
                    "command": "shell_command",
                    "id": "panic-cleanup",
                    "command_line": json!({
                        "command": format!(
                            "sleep 1; printf late > '{}'",
                            late_effect.display()
                        ),
                        "timeout_ms": 10_000
                    }).to_string(),
                    "step": 1
                }]
            }
        });
        let running = {
            let service = service.clone();
            tokio::spawn(async move {
                service
                    .execute_with_request_id(request, Some("panic-cleanup"))
                    .await
            })
        };

        tokio::time::timeout(Duration::from_secs(5), cleanup_gate.entered.acquire())
            .await
            .expect("panic cleanup must reach the deterministic gate")
            .expect("panic cleanup gate")
            .forget();
        let claim: Value = serde_json::from_slice(
            &std::fs::read(receipt_directory.join("panic-cleanup.claim.json"))
                .expect("explicit-id claim"),
        )
        .expect("explicit-id claim JSON");
        let pid = claim["pid"].as_u64().expect("spawned command pid") as u32;
        assert_eq!(claim["state"], "running", "{claim}");
        assert_eq!(service.active_count_for_session("panic-cleanup-session"), 1);
        assert_eq!(service.cancel_session("panic-cleanup-session"), 1);
        assert!(
            tokio::time::timeout(
                Duration::from_millis(100),
                service.wait_for_session_idle("panic-cleanup-session")
            )
            .await
            .is_err(),
            "Router must remain active while panic cleanup is pending"
        );
        assert!(!receipt_directory.join("panic-cleanup.json").exists());

        cleanup_gate.release.add_permits(1);
        let error = running
            .await
            .expect("outer Router task")
            .expect_err("worker panic must remain a Router error");
        assert!(
            error
                .to_string()
                .contains("ROUTER_COMMAND_RUN_WORKER_FAILED"),
            "{error:#}"
        );
        tokio::time::timeout(
            Duration::from_secs(5),
            service.wait_for_session_idle("panic-cleanup-session"),
        )
        .await
        .expect("Router may become idle only after panic cleanup");
        assert_eq!(service.active_count_for_session("panic-cleanup-session"), 0);

        let claim: Value = serde_json::from_slice(
            &std::fs::read(receipt_directory.join("panic-cleanup.claim.json"))
                .expect("terminal explicit-id claim"),
        )
        .expect("terminal explicit-id claim JSON");
        let receipt: Value = serde_json::from_slice(
            &std::fs::read(receipt_directory.join("panic-cleanup.json"))
                .expect("explicit-id receipt"),
        )
        .expect("explicit-id receipt JSON");
        assert_eq!(claim["execution_count"], 1, "{claim}");
        assert_eq!(claim["state"], "interrupted", "{claim}");
        assert_eq!(receipt["terminal_state"], "interrupted", "{receipt}");
        assert_eq!(receipt["failure_class"], "worker_panic", "{receipt}");
        assert_eq!(receipt["outcome"], "unknown", "{receipt}");
        assert_eq!(receipt["retry_safe"], false, "{receipt}");
        assert_eq!(receipt["reconcile_required"], true, "{receipt}");
        assert_eq!(receipt["process_reaped"], true, "{receipt}");
        assert_eq!(receipt["process_group_empty"], true, "{receipt}");
        assert!(
            !std::process::Command::new("/bin/kill")
                .arg("-0")
                .arg(pid.to_string())
                .status()
                .expect("probe command pid")
                .success(),
            "panic-cleaned command pid {pid} must not remain alive"
        );
        tokio::time::sleep(Duration::from_millis(1_200)).await;
        assert!(
            !late_effect.exists(),
            "command effect continued after panic cleanup"
        );
    }

    #[tokio::test]
    async fn malformed_request_has_zero_router_admission_and_zero_command_effect() {
        let workspace = canonical_tempdir();
        let service = CommandRunService::new();

        service
            .execute_with_request_id(
                json!({
                    "session_id": 42,
                    "session_directory": workspace.path(),
                    "arguments": {"commands": []}
                }),
                Some("zero-admission"),
            )
            .await
            .expect_err("malformed payload must fail before Router admission");

        assert_eq!(service.active_count(), 0);
        assert!(!workspace.path().join(".tura/run/command_receipts").exists());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn same_execution_recovery_does_not_duplicate_command_effect() {
        let workspace = canonical_tempdir();
        let marker = workspace.path().join("effect.txt");
        let service = CommandRunService::new();
        let request = json!({
            "session_id": "same-execution-session",
            "session_directory": workspace.path(),
            "arguments": {
                "commands": [{
                    "command": "shell_command",
                    "command_line": json!({
                        "command": format!("printf x >> {}", marker.display()),
                        "timeout_ms": 3_000
                    }).to_string(),
                    "step": 1
                }]
            }
        });

        let first = service
            .execute_with_request_id(request.clone(), Some("same-execution"))
            .await
            .expect("first execution");
        let recovery = service
            .execute_with_request_id(request, Some("same-execution"))
            .await
            .expect("recovery must return a fail-closed command result");

        assert_eq!(first["result"]["results"][0]["success"], true, "{first}");
        assert_eq!(
            recovery["result"]["results"][0]["success"], false,
            "{recovery}"
        );
        assert_eq!(std::fs::read_to_string(&marker).expect("marker"), "x");
        let claim = read_command_record(
            &workspace.path().join(".tura/run/command_receipts"),
            "same-execution",
            0,
            true,
        );
        assert_eq!(claim["execution_count"], 1, "{claim}");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn out_of_order_completion_returns_deterministic_command_order() {
        let workspace = canonical_tempdir();
        let service = CommandRunService::new();
        let commands = [300, 0, 150]
            .into_iter()
            .enumerate()
            .map(|(index, delay_ms)| {
                json!({
                    "command": "shell_command",
                    "command_line": json!({
                        "command": delayed_read_only_command(&format!("ordered-{index}"), delay_ms),
                        "timeout_ms": 3_000
                    }).to_string(),
                    "step": 1
                })
            })
            .collect::<Vec<_>>();

        let response = service
            .execute_with_request_id(
                json!({
                    "session_id": "ordered-session",
                    "session_directory": workspace.path(),
                    "arguments": {"commands": commands}
                }),
                Some("ordered-execution"),
            )
            .await
            .expect("ordered execution");
        let results = response["result"]["results"]
            .as_array()
            .expect("command results");
        assert_eq!(results.len(), 3, "{response}");
        for (index, result) in results.iter().enumerate() {
            assert_eq!(
                result["output"]["terminal_receipt"]["call_id"],
                format!("ordered-execution:step:1:index:{index}"),
                "{response}"
            );
            assert!(
                result["output"]["stdout"]
                    .as_str()
                    .is_some_and(|stdout| stdout.contains(&format!("ordered-{index}"))),
                "{response}"
            );
        }
    }

    #[tokio::test]
    async fn reserved_session_decode_failure_releases_command_admission() {
        let service = CommandRunService::new();
        let reservation = service.reserve_for_session(Some("reserved-session"));
        assert_eq!(service.active_count_for_session("reserved-session"), 1);

        service
            .execute_with_reserved_session(json!({"invalid": true}), None, reservation)
            .await
            .expect_err("invalid reserved command payload must fail");

        assert_eq!(service.active_count(), 0);
        assert_eq!(service.active_count_for_session("reserved-session"), 0);
    }

    #[test]
    fn command_run_payload_deserializes_allowed_commands_as_set() {
        let request: CommandRunRequest = serde_json::from_value(json!({
            "session_directory": ".",
            "arguments": { "commands": [] },
            "allowed_commands": ["shell_command", "shell_command", "task_status"]
        }))
        .expect("payload shape");

        assert_eq!(
            request.allowed_commands,
            Some(BTreeSet::from([
                "shell_command".to_string(),
                "task_status".to_string()
            ]))
        );
        assert!(request.command_env.is_empty());
        assert!(!request.sandbox);
    }

    #[test]
    fn command_run_payload_deserializes_task_scoped_command_environment() {
        let request: CommandRunRequest = serde_json::from_value(json!({
            "session_directory": ".",
            "arguments": { "commands": [] },
            "command_env": {
                "TURA_FORCED_CAPABILITY_DIRECTORIES": "[\"C:/commands/mcp\"]",
                "TURA_MCP_SERVER_NAME": "tura_filesystem"
            }
        }))
        .expect("payload shape");

        assert_eq!(
            request
                .command_env
                .get("TURA_MCP_SERVER_NAME")
                .map(String::as_str),
            Some("tura_filesystem")
        );
    }

    #[tokio::test]
    async fn command_run_service_handles_read_only_requests_concurrently() {
        let workspace = canonical_tempdir();
        let service = CommandRunService::new();
        let request = |label: &str| {
            json!({
                "session_id": format!("session-{label}"),
                "runtime_id": format!("runtime-{label}"),
                "session_directory": workspace.path().display().to_string(),
                "arguments": {
                    "commands": [{
                        "step": 1,
                        "command": "shell_command",
                        "command_line": json!({
                            "command": delayed_read_only_command(label, CONCURRENT_FIXTURE_DELAY_MS),
                            "timeout_ms": READ_ONLY_FIXTURE_TIMEOUT_MS
                        }).to_string()
                    }]
                }
            })
        };

        let sequential_started = Instant::now();
        let seq_first = service
            .execute_with_request_id(request("seq-first"), Some("seq-first-execution"))
            .await
            .expect("sequential first command_run should finish");
        let seq_second = service
            .execute_with_request_id(request("seq-second"), Some("seq-second-execution"))
            .await
            .expect("sequential second command_run should finish");
        let sequential_elapsed = sequential_started.elapsed();
        assert_eq!(
            seq_first["result"]["results"][0]["success"], true,
            "sequential first command_run should succeed: {seq_first}"
        );
        assert_eq!(
            seq_second["result"]["results"][0]["success"], true,
            "sequential second command_run should succeed: {seq_second}"
        );

        let concurrent_started = Instant::now();
        let (first, second) = tokio::join!(
            service.execute_with_request_id(request("first"), Some("first-execution")),
            service.execute_with_request_id(request("second"), Some("second-execution"))
        );
        let concurrent_elapsed = concurrent_started.elapsed();

        let first = first.expect("first command_run should finish");
        let second = second.expect("second command_run should finish");
        assert_eq!(
            first["result"]["results"][0]["success"], true,
            "first concurrent command_run should succeed: {first}"
        );
        assert_eq!(
            second["result"]["results"][0]["success"], true,
            "second concurrent command_run should succeed: {second}"
        );
        let overlap_margin = Duration::from_millis(CONCURRENT_FIXTURE_DELAY_MS / 2);
        assert!(
            concurrent_elapsed + overlap_margin < sequential_elapsed,
            "read-only command_run requests should overlap instead of serializing; sequential_elapsed={sequential_elapsed:?}; concurrent_elapsed={concurrent_elapsed:?}"
        );
    }

    #[tokio::test]
    async fn jspace_admission_reuses_same_digest_and_rejects_changed_digest() {
        let workspace = canonical_tempdir();
        let service = CommandRunService::new();
        let contract = jspace_contract(workspace.path());
        let request = |contract: Value| {
            json!({
                "session_id": "jspace-session",
                "runtime_id": "jspace-runtime",
                "session_directory": workspace.path().display().to_string(),
                "jspace_contract": contract,
                "arguments": {
                    "commands": [{
                        "command": "task_status",
                        "command_line": "{\"status\":\"done\"}"
                    }]
                }
            })
        };

        let first = service
            .execute(request(contract.clone()))
            .await
            .expect("first request");
        let second = service
            .execute(request(contract.clone()))
            .await
            .expect("second request");
        assert_eq!(first["result"]["results"][0]["success"], true);
        assert_eq!(second["result"]["results"][0]["success"], true);
        assert_eq!(service.jspace_admissions(), 1);

        let mut changed = contract.clone();
        changed["read_scopes"] = json!(["other/**"]);
        let changed = reseal_jspace(changed);
        let error = service
            .execute(request(changed))
            .await
            .expect_err("changed contract must be rejected");
        assert!(error.to_string().contains("JSPACE_CONTRACT_CHANGED"));
    }

    #[tokio::test]
    async fn jspace_expansion_is_reported_before_apply_patch_mutation() {
        let workspace = canonical_tempdir();
        let service = CommandRunService::new();
        let contract = jspace_contract(workspace.path());
        let response = service
            .execute(json!({
                "session_id": "jspace-expansion-session",
                "runtime_id": "jspace-expansion-runtime",
                "session_directory": workspace.path().display().to_string(),
                "jspace_contract": contract,
                "arguments": {
                    "commands": [{
                        "command": "apply_patch",
                        "command_line": "*** Begin Patch\n*** Add File: outside.txt\n+must-not-exist\n*** End Patch"
                    }]
                }
            }))
            .await
            .expect("expansion is a command result, not an IPC failure");

        assert_eq!(
            response["result"]["results"][0]["jspace_error_code"],
            "JSPACE_EXPANSION_REQUIRED"
        );
        assert!(!workspace.path().join("outside.txt").exists());
    }

    #[tokio::test]
    async fn jspace_patch_without_shell_grant_still_checks_exact_target() {
        let workspace = canonical_tempdir();
        let source = workspace.path().join("src/main.rs");
        std::fs::create_dir(workspace.path().join("src")).expect("source directory");
        std::fs::write(&source, "old\n").expect("source file");
        let service = CommandRunService::new();
        let mut contract = jspace_contract(workspace.path());
        contract["allowed_operations"] = json!(["read", "modify"]);
        contract["denied_operations"] = json!([
            "command",
            "create",
            "delete",
            "network",
            "install",
            "system_mutation"
        ]);
        contract["command_templates"] = json!([]);
        let contract = reseal_jspace(contract);

        let request = |command_line: &str| {
            json!({
                "session_id": "jspace-no-shell-patch-session",
                "runtime_id": "jspace-no-shell-patch-runtime",
                "session_directory": workspace.path().display().to_string(),
                "jspace_contract": contract,
                "arguments": {
                    "commands": [{"command": "apply_patch", "command_line": command_line}]
                }
            })
        };
        let allowed = service
            .execute(request(
                "*** Begin Patch\n*** Update File: src/main.rs\n@@\n-old\n+new\n*** End Patch",
            ))
            .await
            .expect("declared patch request");
        assert_eq!(allowed["result"]["results"][0]["success"], true);
        assert_eq!(
            std::fs::read_to_string(&source).expect("updated source"),
            "new\n"
        );

        let undeclared = workspace.path().join("src/other.rs");
        std::fs::write(&undeclared, "old\n").expect("undeclared source file");
        let denied = service
            .execute(request("*** Begin Patch\n*** Update File: src/other.rs\n@@\n-old\n+must-not-exist\n*** End Patch"))
            .await
            .expect("denial is a command result");
        assert_eq!(
            denied["result"]["results"][0]["jspace_error_code"],
            "JSPACE_EXPANSION_REQUIRED"
        );
        assert_eq!(
            std::fs::read_to_string(undeclared).expect("unchanged source"),
            "old\n"
        );
    }

    #[tokio::test]
    async fn jspace_empty_write_scope_rejects_declared_apply_patch_before_mutation() {
        let workspace = canonical_tempdir();
        std::fs::create_dir(workspace.path().join("src")).expect("create source directory");
        let service = CommandRunService::new();
        let mut contract = jspace_contract(workspace.path());
        contract["write_scopes"] = json!([]);
        let contract = reseal_jspace(contract);
        let marker = workspace.path().join("src/main.rs");
        let response = service
            .execute(json!({
                "session_id": "jspace-command-session",
                "runtime_id": "jspace-command-runtime",
                "session_directory": workspace.path().display().to_string(),
                "jspace_contract": contract,
                "arguments": {
                    "commands": [{
                        "command": "apply_patch",
                        "command_line": "*** Begin Patch\n*** Add File: src/main.rs\n+must-not-exist\n*** End Patch"
                    }]
                }
            }))
            .await
            .expect("denial is a command result");

        assert_eq!(
            response["result"]["results"][0]["jspace_error_code"],
            "JSPACE_EXPANSION_REQUIRED"
        );
        assert!(!marker.exists());
    }

    // 2026-10-22 i8: a Luna worker sent id-only apply_patch entries as labels next to the real
    // patches; each label was rejected before execution as a malformed patch.
    #[tokio::test]
    async fn jspace_admits_an_empty_apply_patch_label_beside_a_real_patch() {
        let workspace = canonical_tempdir();
        std::fs::create_dir(workspace.path().join("src")).expect("create source directory");
        let service = CommandRunService::new();
        let contract = jspace_contract(workspace.path());
        let response = service
            .execute(json!({
                "session_id": "jspace-label-session",
                "runtime_id": "jspace-label-runtime",
                "session_directory": workspace.path().display().to_string(),
                "jspace_contract": contract,
                "arguments": {
                    "commands": [
                        {"command": "apply_patch", "id": "label", "step": 1},
                        {"command": "apply_patch", "step": 1,
                         "command_line": "*** Begin Patch\n*** Add File: src/main.rs\n+fn main() {}\n*** End Patch"}
                    ]
                }
            }))
            .await
            .expect("command result");

        let results = &response["result"]["results"];
        assert_eq!(results[0]["success"], true, "{response}");
        assert!(results[0].to_string().contains("no_effect"), "{response}");
        assert_eq!(results[1]["success"], true, "{response}");
        assert_eq!(
            std::fs::read_to_string(workspace.path().join("src/main.rs")).expect("patched file"),
            "fn main() {}\n"
        );
    }

    #[tokio::test]
    async fn jspace_rejects_shell_suffix_injection_before_execution() {
        let workspace = canonical_tempdir();
        let service = CommandRunService::new();
        let marker = workspace.path().join("injected.txt");
        let response = service
            .execute(json!({
                "session_id": "jspace-injection-session",
                "runtime_id": "jspace-injection-runtime",
                "session_directory": workspace.path().display().to_string(),
                "jspace_contract": jspace_contract(workspace.path()),
                "arguments": {
                    "commands": [{
                        "command": "shell_command",
                        "command_line": serde_json::to_string(&json!({
                            "command": format!(
                                "git status --short; touch {}",
                                marker.display()
                            )
                        })).expect("shell args")
                    }]
                }
            }))
            .await
            .expect("denial is a command result");

        assert_eq!(
            response["result"]["results"][0]["jspace_error_code"],
            "JSPACE_COMMAND_SYNTAX_UNSAFE"
        );
        assert!(!marker.exists());
    }

    fn delayed_read_only_command(label: &str, delay_ms: u64) -> String {
        if cfg!(windows) {
            format!("Test-Path .; Start-Sleep -Milliseconds {delay_ms}; Write-Output {label}")
        } else {
            format!(
                "find . -maxdepth 0; sleep {}.{:03}; printf {label}",
                delay_ms / 1000,
                delay_ms % 1000
            )
        }
    }

    async fn wait_for_command_record_count(directory: &Path, claims: bool, expected: usize) {
        tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                let count = std::fs::read_dir(directory)
                    .ok()
                    .into_iter()
                    .flatten()
                    .filter_map(Result::ok)
                    .filter(|entry| {
                        let name = entry.file_name();
                        let name = name.to_string_lossy();
                        if claims {
                            name.ends_with(".claim.json")
                        } else {
                            name.ends_with(".json") && !name.ends_with(".claim.json")
                        }
                    })
                    .count();
                if count == expected {
                    return;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("command record count did not converge");
    }

    fn command_record_path(
        directory: &Path,
        execution_id: &str,
        index: usize,
        claim: bool,
    ) -> std::path::PathBuf {
        let identity = format!(
            "{execution_id}:step:{}:index:{index}",
            if index < 5 { 1 } else { index - 3 }
        );
        let encoded = identity.replace(':', "_x3a_");
        let suffix = if claim { ".claim.json" } else { ".json" };
        directory.join(format!("{encoded}{suffix}"))
    }

    fn read_command_record(
        directory: &Path,
        execution_id: &str,
        index: usize,
        claim: bool,
    ) -> Value {
        let path = command_record_path(directory, execution_id, index, claim);
        serde_json::from_slice(&std::fs::read(&path).expect("command record"))
            .expect("command record JSON")
    }
}
