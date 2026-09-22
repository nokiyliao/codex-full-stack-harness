//! Runtime worker entry: the body of the standalone `tura_runtime` binary.
//!
//! Refactor stage 3: the worker is its own binary (`crates/runtime` →
//! `tura_runtime`), no longer the gateway binary re-invoked by role. The router
//! spawns it and drives it over the line protocol: read `{ "kind", "payload" }`
//! per line, write one JSON reply per line. Router-managed runtime workers run
//! in one-shot mode so they exit after a single `call` envelope.
//!
//! Boundary: runtime stays a library; this entry only hosts the worker process,
//! activates the agent spec the router hands down, and runs one prompt. Session
//! state still flows through the session_db socket (never `open_default`).

use std::collections::BTreeSet;
use std::io::{BufRead, Write};
use std::path::PathBuf;
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use lifecycle::{
    ProviderConfig, RuntimeAggregate, RuntimeCommand, RuntimeError, RuntimeProjection,
    RuntimeProviderConfig, RuntimeState, ToolCallRecord, ToolChoice, UsageReport,
};
use runtime_contract::{
    LifecycleExecutionContext, NativeCodexExecutionBinding, NativeCodexTerminalEnvelope,
    NativeCodexTerminalState, NativeCodexToolObservation, TaskContextCapsule,
};
use runtime_contract::{WORKER_KIND_CALL, WORKER_KIND_HEALTH_CHECK, WorkerEnvelope};
use serde_json::{Value, json};
use session_log_contract::SessionFeedEvent;
use sysinfo::{Pid, ProcessRefreshKind, RefreshKind, System};

use crate::mano::ManoProcessResult;
use crate::native_codex_runner::{
    NativeCodexCommandGraph, NativeCodexContext, NativeCodexRunRequest, NativeCodexRunner,
};
use crate::runtime_event_writer::RuntimeEventWriter;
use lifecycle::SessionState;
use lifecycle::{SessionInput, SessionLogEntry};

/// Run the runtime worker loop: blocking read on stdin, write on stdout, until
/// the peer closes or one-shot mode completes a call.
pub fn run() -> std::io::Result<()> {
    start_router_parent_watchdog();
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout();
    let mut line = String::new();
    let mut reader = stdin.lock();

    loop {
        line.clear();
        let read = reader.read_line(&mut line)?;
        if read == 0 {
            return Ok(());
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }

        let parsed = serde_json::from_str::<WorkerEnvelope>(trimmed);
        let is_call = parsed
            .as_ref()
            .is_ok_and(|envelope| envelope.kind == WORKER_KIND_CALL);
        let response = match parsed {
            Ok(envelope) => handle_envelope(&envelope),
            Err(error) => json!({ "ok": false, "error": format!("invalid envelope: {error}") }),
        };

        let encoded = serde_json::to_string(&response)
            .unwrap_or_else(|error| format!("{{\"ok\":false,\"error\":\"{error}\"}}"));
        stdout.write_all(encoded.as_bytes())?;
        stdout.write_all(b"\n")?;
        stdout.flush()?;
        if runtime_oneshot_enabled() && is_call {
            return Ok(());
        }
    }
}

fn runtime_oneshot_enabled() -> bool {
    std::env::var("TURA_RUNTIME_ONESHOT")
        .ok()
        .is_some_and(|value| env_flag(&value))
}

fn start_router_parent_watchdog() {
    let Some(parent) = RouterParentProcess::from_env() else {
        return;
    };
    std::thread::spawn(move || {
        loop {
            std::thread::sleep(std::time::Duration::from_millis(500));
            if !parent.is_alive() {
                std::process::exit(70);
            }
        }
    });
}

#[derive(Debug, Clone, Copy)]
struct RouterParentProcess {
    pid: u32,
    start_time: Option<u64>,
}

impl RouterParentProcess {
    fn from_env() -> Option<Self> {
        let pid = std::env::var("TURA_ROUTER_PARENT_PID")
            .ok()
            .and_then(|value| value.trim().parse::<u32>().ok())
            .filter(|pid| *pid > 0)?;
        let start_time = std::env::var("TURA_ROUTER_PARENT_START_TIME")
            .ok()
            .and_then(|value| value.trim().parse::<u64>().ok());
        Some(Self { pid, start_time })
    }

    fn is_alive(&self) -> bool {
        let mut system = System::new_with_specifics(
            RefreshKind::new().with_processes(ProcessRefreshKind::new()),
        );
        system.refresh_processes();
        let Some(process) = system.process(Pid::from_u32(self.pid)) else {
            return false;
        };
        self.start_time
            .map(|expected| process.start_time() == expected)
            .unwrap_or(true)
    }
}

fn env_flag(value: &str) -> bool {
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn no_op_manual_enabled_from_env() -> bool {
    std::env::var("TURA_NO_OP_MANUAL")
        .ok()
        .is_some_and(|value| env_flag(&value))
}

fn handle_envelope(envelope: &WorkerEnvelope) -> Value {
    match envelope.kind.as_str() {
        WORKER_KIND_HEALTH_CHECK => json!({
            "ok": true,
            "role": "runtime_worker",
            "version": tura_path::instance_version(),
        }),
        WORKER_KIND_CALL => handle_call(&envelope.payload),
        other => json!({ "ok": false, "error": format!("unsupported kind: {other}") }),
    }
}

/// Call payload shape: `{ "input": { "method", "input": <RuntimeWorkerCall> } }`.
/// This matches the router `invoke_persistent` envelope.
fn handle_call(payload: &Value) -> Value {
    let call = payload
        .get("input")
        .and_then(|value| value.get("input"))
        .cloned()
        .unwrap_or_else(|| payload.clone());

    let Some(session_id) = call
        .get("session_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
    else {
        return json!({
            "ok": false,
            "code": "LIFECYCLE_IDENTITY_MISSING",
            "error": "missing session_id"
        });
    };
    let Some(runtime_id) = call
        .get("runtime_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
    else {
        return json!({ "ok": false, "session_id": session_id, "error": "missing runtime_id" });
    };
    let Some(lease_id) = call
        .get("lease_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
    else {
        return json!({ "ok": false, "session_id": session_id, "error": "missing lease_id" });
    };
    let fallback_from_id = match call.get("fallback_from_id") {
        None | Some(Value::Null) => None,
        Some(Value::String(value)) if !value.trim().is_empty() => Some(value.trim().to_string()),
        Some(_) => {
            return json!({
                "ok": false,
                "session_id": session_id,
                "code": "RUNTIME_RETRY_SOURCE_INVALID",
                "error": "fallback_from_id must be a non-empty string when provided"
            });
        }
    };
    let directory = call
        .get("directory")
        .and_then(Value::as_str)
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
    let prompt = call
        .get("prompt")
        .or_else(|| call.get("message"))
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_default();
    if prompt.trim().is_empty() {
        return json!({ "ok": false, "session_id": session_id, "error": "empty prompt" });
    }
    let lifecycle = match call.get("lifecycle").cloned() {
        Some(value) => match serde_json::from_value::<LifecycleExecutionContext>(value) {
            Ok(context)
                if !context.transaction_id.trim().is_empty()
                    && !context.commander_session_id.trim().is_empty() =>
            {
                context
            }
            Ok(_) => {
                return json!({
                    "ok": false,
                    "session_id": session_id,
                    "code": "LIFECYCLE_IDENTITY_MISSING",
                    "error": "lifecycle transaction_id and commander_session_id are required"
                });
            }
            Err(error) => {
                return json!({
                    "ok": false,
                    "session_id": session_id,
                    "code": "LIFECYCLE_CONTEXT_INVALID",
                    "error": format!("invalid lifecycle context: {error}")
                });
            }
        },
        None => {
            return json!({
                "ok": false,
                "session_id": session_id,
                "code": "LIFECYCLE_IDENTITY_MISSING",
                "error": "missing lifecycle context"
            });
        }
    };
    let agent = call
        .get("agent")
        .and_then(Value::as_str)
        .map(str::to_string);
    let mut runtime_context = call
        .get("runtime_context")
        .and_then(Value::as_str)
        .map(str::to_string);
    let no_op_manual = call
        .get("no_op_manual")
        .and_then(Value::as_bool)
        .unwrap_or_else(no_op_manual_enabled_from_env);
    let planning_mode_override = call.get("planning_mode_override").and_then(Value::as_bool);
    let return_log = call
        .get("return_log")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let agent_spec = call
        .get("agent_spec")
        .filter(|value| !value.is_null())
        .cloned();
    let jspace_contract = call.get("jspace_contract").cloned();
    let task_context_capsule = match call.get("task_context_capsule").cloned() {
        None | Some(Value::Null) => None,
        Some(value) => match TaskContextCapsule::from_value(value) {
            Ok(capsule) => Some(capsule),
            Err(error) => {
                return json!({
                    "ok": false,
                    "session_id": session_id,
                    "code": error.split(':').next().unwrap_or("TASK_CONTEXT_CAPSULE_INVALID"),
                    "error": error,
                });
            }
        },
    };
    if let Some(capsule) = task_context_capsule.as_ref() {
        if runtime_context.is_some() {
            return json!({
                "ok": false,
                "session_id": session_id,
                "code": "TASK_CONTEXT_RUNTIME_CONTEXT_CONFLICT",
                "error": "raw runtime_context cannot accompany a signed Task Context Capsule",
            });
        }
        if agent_spec.is_none() {
            return json!({
                "ok": false,
                "session_id": session_id,
                "code": "TASK_CONTEXT_AGENT_SPEC_MISSING",
                "error": "a signed Task Context Capsule requires a router-resolved agent_spec",
            });
        }
        if let Err(error) = capsule.bind_jspace(jspace_contract.as_ref()) {
            return json!({
                "ok": false,
                "session_id": session_id,
                "code": error.split(':').next().unwrap_or("TASK_CONTEXT_JSPACE_BINDING_INVALID"),
                "error": error,
            });
        }
        runtime_context = Some(capsule.provider_context());
    }

    let native_codex_execution = match call.get("native_codex_execution").cloned() {
        None | Some(Value::Null) => None,
        Some(value) => match NativeCodexExecutionBinding::from_value(value) {
            Ok(binding) => Some(binding),
            Err(error) => {
                return json!({
                    "ok": false,
                    "session_id": session_id,
                    "code": error
                        .split(':')
                        .next()
                        .unwrap_or("NATIVE_CODEX_EXECUTION_BINDING_INVALID"),
                    "error": error,
                });
            }
        },
    };
    if let Some(binding) = native_codex_execution {
        let Some(capsule) = task_context_capsule else {
            return json!({
                "ok": false,
                "session_id": session_id,
                "code": "NATIVE_CODEX_DURABLE_CAPSULE_MISSING",
                "error": "Native Codex execution requires the canonical task context capsule",
            });
        };
        return handle_native_codex_call(NativeCodexWorkerCall {
            session_id,
            runtime_id,
            lease_id,
            fallback_from_id,
            directory,
            prompt,
            lifecycle,
            capsule,
            binding,
            jspace_contract: jspace_contract
                .expect("TaskContextCapsule::bind_jspace requires a J-Space contract"),
        });
    }

    let input = SessionInput {
        user_input: prompt,
        file_input: Vec::new(),
        agent,
        runtime_context,
        planning_mode_override,
    };

    if no_op_manual {
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_NO_OP_MANUAL", "1")
        };
    } else {
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_NO_OP_MANUAL")
        };
    }

    if let Some(agent_spec) = agent_spec {
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_ROUTER_AGENT_SPEC", agent_spec.to_string())
        };
    } else {
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::remove_var("TURA_ROUTER_AGENT_SPEC")
        };
    }
    match crate::mano::process_from_gateway_session_with_lease_and_lifecycle_and_jspace_in_directory(
        session_id.clone(),
        runtime_id,
        fallback_from_id,
        lease_id,
        input,
        directory,
        lifecycle,
        jspace_contract,
    ) {
        Ok(result) => response_from_mano_result(&session_id, result, return_log),
        Err(error) => json!({ "ok": false, "session_id": session_id, "error": error }),
    }
}

struct NativeCodexWorkerCall {
    session_id: String,
    runtime_id: String,
    lease_id: String,
    fallback_from_id: Option<String>,
    directory: PathBuf,
    prompt: String,
    lifecycle: LifecycleExecutionContext,
    capsule: TaskContextCapsule,
    binding: NativeCodexExecutionBinding,
    jspace_contract: Value,
}

fn handle_native_codex_call(call: NativeCodexWorkerCall) -> Value {
    let task_id = match call
        .lifecycle
        .task_id
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        Some(task_id) => task_id.to_string(),
        None => {
            return native_worker_error(
                &call.session_id,
                "NATIVE_CODEX_DURABLE_TASK_ID_MISSING",
                "Native Codex execution requires the durable lifecycle task identity",
            );
        }
    };
    let Some(mission_revision_sha256) = call.lifecycle.parent_mission_revision_sha256.as_deref()
    else {
        return native_worker_error(
            &call.session_id,
            "NATIVE_CODEX_DURABLE_MISSION_REVISION_MISSING",
            "Native Codex execution requires the durable mission revision",
        );
    };
    if let Err(error) = call.binding.bind_authoritative_task(
        &call.capsule,
        &task_id,
        mission_revision_sha256,
        &call.prompt,
    ) {
        return native_worker_error(
            &call.session_id,
            error
                .split(':')
                .next()
                .unwrap_or("NATIVE_CODEX_DURABLE_BINDING_INVALID"),
            &error,
        );
    }

    let now = Utc::now();
    let provider = native_codex_provider_config(call.binding.timeout_ms);
    let mut runtime = match RuntimeAggregate::new_with_fallback(
        call.runtime_id.clone(),
        call.session_id.clone(),
        "native-codex-thin-kernel".to_string(),
        provider,
        now,
        call.fallback_from_id.clone(),
    ) {
        Ok(runtime) => runtime,
        Err(error) => {
            return native_worker_error(
                &call.session_id,
                "NATIVE_CODEX_RUNTIME_CREATE_FAILED",
                &error,
            );
        }
    };
    for command in [
        RuntimeCommand::CallStarted { called_at: now },
        RuntimeCommand::WaitingFirstToken,
        RuntimeCommand::CaptureInput {
            input: json!({
                "schema_version": "tura_native_codex_durable_input_v1",
                "execution_binding_sha256": call.binding.semantic_sha256,
                "task_context_capsule_sha256": call.capsule.semantic_sha256,
                "task_delta_sha256": call.binding.task_delta.semantic_sha256,
            }),
        },
    ] {
        if let Err(error) = runtime.execute(command) {
            return native_worker_error(
                &call.session_id,
                "NATIVE_CODEX_RUNTIME_EVENT_INVALID",
                &error,
            );
        }
    }

    let mut writer = match RuntimeEventWriter::new_with_lifecycle(
        call.session_id.clone(),
        call.runtime_id.clone(),
        call.lease_id.clone(),
        Some(call.lifecycle.clone()),
    ) {
        Ok(writer) => writer,
        Err(error) => {
            return native_worker_error(
                &call.session_id,
                "NATIVE_CODEX_RUNTIME_WRITER_UNAVAILABLE",
                &error,
            );
        }
    };
    if let Err(error) = writer.flush(&mut runtime) {
        return native_worker_error(
            &call.session_id,
            "NATIVE_CODEX_RUNTIME_START_PERSIST_FAILED",
            &error,
        );
    }
    let feed = match writer.feed_publisher(&call.runtime_id, &call.session_id) {
        Ok(feed) => feed,
        Err(error) => {
            return native_worker_error(
                &call.session_id,
                "NATIVE_CODEX_RUNTIME_FEED_UNAVAILABLE",
                &error,
            );
        }
    };

    let runner_request = NativeCodexRunRequest {
        codex_executable: PathBuf::from(&call.binding.codex_executable),
        codex_executable_sha256: call.binding.codex_executable_sha256.clone(),
        workspace: call.directory,
        session_id: call.session_id.clone(),
        task_id,
        execution_id: call.runtime_id.clone(),
        lease_id: call.lease_id.clone(),
        context: NativeCodexContext {
            provider_profile: None,
            execution_profile_sha256: call.binding.execution_profile_sha256.clone(),
            execution_binding_sha256: call.binding.semantic_sha256.clone(),
            expected_task_context_capsule_sha256: call.capsule.semantic_sha256.clone(),
            expected_task_delta_sha256: call.binding.task_delta.semantic_sha256.clone(),
            expected_jspace_semantic_sha256: call.capsule.jspace_semantic_sha256.clone(),
            task_context_capsule: call.capsule,
            task_delta: call.binding.task_delta.clone(),
        },
        sandbox: call.binding.sandbox,
        timeout: Duration::from_millis(call.binding.timeout_ms),
        command_graph: Some(NativeCodexCommandGraph {
            executable: PathBuf::from(&call.binding.command_graph_executable),
            executable_sha256: call.binding.command_graph_executable_sha256.clone(),
            allowed_commands: call
                .binding
                .command_graph_allowed_commands
                .iter()
                .cloned()
                .collect::<BTreeSet<_>>(),
            jspace_contract: call.jspace_contract,
        }),
    };
    let started = Instant::now();
    let terminal = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(native_runtime) => native_runtime.block_on(NativeCodexRunner::run(runner_request)),
        Err(error) => Err(format!("NATIVE_CODEX_ASYNC_RUNTIME_CREATE_FAILED:{error}")),
    };
    let elapsed_ms = u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX);
    finish_native_runtime(
        &call.session_id,
        &call.runtime_id,
        &mut runtime,
        &mut writer,
        &feed,
        terminal,
        elapsed_ms,
    )
}

fn finish_native_runtime(
    session_id: &str,
    runtime_id: &str,
    runtime: &mut RuntimeAggregate,
    writer: &mut RuntimeEventWriter,
    feed: &crate::runtime_event_writer::RuntimeFeedPublisher,
    terminal: Result<NativeCodexTerminalEnvelope, String>,
    elapsed_ms: u64,
) -> Value {
    let finished_at = Utc::now();
    let (ok, visible_text, terminal_value) = match terminal {
        Ok(envelope) => {
            let usage = native_usage(envelope.usage.as_ref(), elapsed_ms);
            let terminal_value = serde_json::to_value(&envelope)
                .unwrap_or_else(|error| json!({"terminal_encode_error": error.to_string()}));
            if let Err(error) = runtime.execute(RuntimeCommand::CaptureOutput {
                output: terminal_value.clone(),
            }) {
                return native_worker_error(
                    session_id,
                    "NATIVE_CODEX_RUNTIME_OUTPUT_INVALID",
                    &error,
                );
            }
            for observation in &envelope.tool_observations {
                let record = native_tool_call_record(observation, finished_at);
                if let Err(error) = runtime.execute(RuntimeCommand::RecordToolCall { record }) {
                    return native_worker_error(
                        session_id,
                        "NATIVE_CODEX_RUNTIME_TOOL_EVENT_INVALID",
                        &error,
                    );
                }
            }
            let visible_text = envelope
                .final_text
                .clone()
                .or_else(|| envelope.error.clone())
                .unwrap_or_else(|| format!("Native Codex terminal: {:?}", envelope.terminal_state));
            let terminal_command = match envelope.terminal_state {
                NativeCodexTerminalState::Completed => {
                    if let Err(error) = runtime.execute(RuntimeCommand::FirstTokenReceived {
                        first_token_at: finished_at,
                    }) {
                        return native_worker_error(
                            session_id,
                            "NATIVE_CODEX_RUNTIME_FIRST_TOKEN_INVALID",
                            &error,
                        );
                    }
                    if let Err(error) = runtime.execute(RuntimeCommand::AppendText {
                        chunk: visible_text.clone(),
                    }) {
                        return native_worker_error(
                            session_id,
                            "NATIVE_CODEX_RUNTIME_TEXT_INVALID",
                            &error,
                        );
                    }
                    RuntimeCommand::FinishSuccess { finished_at, usage }
                }
                NativeCodexTerminalState::Failed | NativeCodexTerminalState::Interrupted => {
                    RuntimeCommand::FinishFailure {
                        finished_at,
                        error: RuntimeError {
                            error_code: Some(match envelope.terminal_state {
                                NativeCodexTerminalState::Interrupted => {
                                    "NATIVE_CODEX_INTERRUPTED".to_string()
                                }
                                _ => "NATIVE_CODEX_FAILED".to_string(),
                            }),
                            error_text: envelope.error.clone(),
                            retry_allowed: false,
                            fallback_allowed: false,
                            fallback_to_id: None,
                        },
                        terminal_state: match envelope.terminal_state {
                            NativeCodexTerminalState::Interrupted => RuntimeState::TimedOut,
                            _ => RuntimeState::Failed,
                        },
                        usage,
                    }
                }
            };
            if let Err(error) = runtime.execute(terminal_command) {
                return native_worker_error(
                    session_id,
                    "NATIVE_CODEX_RUNTIME_TERMINAL_EVENT_INVALID",
                    &error,
                );
            }
            (
                envelope.terminal_state == NativeCodexTerminalState::Completed,
                visible_text,
                terminal_value,
            )
        }
        Err(error) => {
            if let Err(event_error) = runtime.execute(RuntimeCommand::FinishFailure {
                finished_at,
                error: RuntimeError {
                    error_code: Some(
                        error
                            .split(':')
                            .next()
                            .unwrap_or("NATIVE_CODEX_RUNNER_FAILED")
                            .to_string(),
                    ),
                    error_text: Some(error.clone()),
                    retry_allowed: false,
                    fallback_allowed: false,
                    fallback_to_id: None,
                },
                terminal_state: RuntimeState::Failed,
                usage: None,
            }) {
                return native_worker_error(
                    session_id,
                    "NATIVE_CODEX_RUNTIME_FAILURE_EVENT_INVALID",
                    &event_error,
                );
            }
            (false, error.clone(), json!({"error": error}))
        }
    };

    if let Err(error) = writer.flush(runtime) {
        return native_worker_error(
            session_id,
            "NATIVE_CODEX_RUNTIME_TERMINAL_PERSIST_FAILED",
            &error,
        );
    }
    let now_ms = finished_at.timestamp_millis();
    if let Err(error) = feed.publish(SessionFeedEvent::AgentMessage {
        message_id: format!("native-codex-message-{runtime_id}"),
        part_id: format!("native-codex-part-{runtime_id}"),
        reply_message: visible_text.clone(),
        new_learning: String::new(),
        runtime_status: Some(RuntimeProjection::new(
            runtime_id.to_string(),
            runtime.state,
        )),
        context_tokens: Some(runtime.context_tokens),
        usage: runtime.usage.clone(),
        created_at: now_ms,
        updated_at: now_ms,
    }) {
        return native_worker_error(
            session_id,
            "NATIVE_CODEX_RUNTIME_FEED_PUBLISH_FAILED",
            &error,
        );
    }
    if let Err(error) = writer.seal_runtime(runtime_id) {
        return native_worker_error(session_id, "NATIVE_CODEX_RUNTIME_SEAL_FAILED", &error);
    }
    json!({
        "ok": ok,
        "session_id": session_id,
        "runtime_id": runtime_id,
        "session_state": if ok { "completed" } else { "failed" },
        "final_text": visible_text,
        "native_codex_terminal": terminal_value,
    })
}

fn native_tool_call_record(
    observation: &NativeCodexToolObservation,
    observed_at: DateTime<Utc>,
) -> ToolCallRecord {
    let tool_reported_success = observation.status == "completed"
        && !observation.is_error
        && observation.exit_code.is_none_or(|code| code == 0);
    ToolCallRecord {
        tool_called_name: observation.tool_name.clone(),
        tool_called_input: json!({"sha256": observation.input_sha256}),
        provider_metadata: Some(json!({
            "effect_id": observation.effect_id,
            "item_id": observation.item_id,
            "item_type": observation.item_type,
            "output_sha256": observation.output_sha256,
            "status": observation.status,
            "is_error": observation.is_error,
            "exit_code": observation.exit_code,
        })),
        tool_received_at: observed_at,
        tool_executed_at: observed_at,
        tool_calldata_received_at: observed_at,
        tool_reported_success,
        agent_reported_success: false,
        agent_reported_helpful: false,
        agent_reported_summary: String::new(),
        validator_reported_success: None,
    }
}

fn native_codex_provider_config(timeout_ms: u64) -> RuntimeProviderConfig {
    RuntimeProviderConfig {
        base: ProviderConfig {
            tura_llm_name: "official_codex_app_server".to_string(),
            default_model_tier: Some("priority".to_string()),
            current_model: Some("gpt-5.6-sol".to_string()),
            stream: true,
            temperature: 0.0,
            max_tokens: 0,
            tool_choice: ToolChoice::Auto,
            time_out_ms: timeout_ms,
        },
        thinking: true,
        provider_name: "official_codex_app_server".to_string(),
        model_name: "gpt-5.6-sol".to_string(),
        provider_url_name: "native_codex_cli".to_string(),
        llm_provider_name: "openai".to_string(),
    }
}

fn native_usage(value: Option<&Value>, elapsed_ms: u64) -> Option<UsageReport> {
    let value = value?;
    let input_tokens = value
        .get("input_tokens")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let output_tokens = value
        .get("output_tokens")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let total_tokens = value
        .get("total_tokens")
        .and_then(Value::as_u64)
        .unwrap_or_else(|| input_tokens.saturating_add(output_tokens));
    Some(UsageReport {
        input_tokens,
        output_tokens,
        total_tokens,
        cached_input_tokens: value
            .get("cached_input_tokens")
            .and_then(Value::as_u64)
            .unwrap_or(0),
        cache_write_tokens: 0,
        reasoning_tokens: value
            .get("reasoning_tokens")
            .and_then(Value::as_u64)
            .unwrap_or(0),
        attachment_input_tokens: 0,
        input_cost: 0.0,
        output_cost: 0.0,
        total_cost: 0.0,
        currency: "USD".to_string(),
        pricing_source: "native_codex_unpriced".to_string(),
        latency_ms: elapsed_ms,
        time_to_first_token_ms: elapsed_ms,
        token_per_second: if elapsed_ms == 0 {
            0.0
        } else {
            output_tokens as f64 * 1_000.0 / elapsed_ms as f64
        },
    })
}

fn native_worker_error(session_id: &str, code: &str, error: &str) -> Value {
    json!({
        "ok": false,
        "session_id": session_id,
        "code": code,
        "error": error,
    })
}

fn response_from_mano_result(
    session_id: &str,
    result: ManoProcessResult,
    return_log: bool,
) -> Value {
    let final_text = final_assistant_text(&result.session.session_log).unwrap_or_default();
    let message_count = result.session.session_log.len();
    let turn_started_at_ms = result.session.session_started_at.timestamp_millis();
    if result.session.state == SessionState::Failed {
        let error = result
            .final_error
            .filter(|error| !error.trim().is_empty())
            .or_else(|| {
                final_text
                    .trim()
                    .is_empty()
                    .then_some("runtime session failed without a final provider error".to_string())
            })
            .unwrap_or_else(|| final_text.clone());
        let mut response = json!({
            "ok": false,
            "session_id": session_id,
            "session_state": result.session.state,
            "message_count": message_count,
            "turn_started_at_ms": turn_started_at_ms,
            "final_text": final_text,
            "error": error,
        });
        if return_log {
            response["session_log"] = json!(result.session.session_log);
        }
        return response;
    }
    if final_text.trim().is_empty() {
        let mut response = json!({
            "ok": false,
            "session_id": session_id,
            "session_state": result.session.state,
            "message_count": message_count,
            "turn_started_at_ms": turn_started_at_ms,
            "error": "runtime completed without a final assistant message",
        });
        if return_log {
            response["session_log"] = json!(result.session.session_log);
        }
        return response;
    }
    let mut response = json!({
        "ok": true,
        "session_id": session_id,
        "session_state": result.session.state,
        "message_count": message_count,
        "turn_started_at_ms": turn_started_at_ms,
        "final_text": final_text,
    });
    if return_log {
        response["session_log"] = json!(result.session.session_log);
    }
    response
}

fn final_assistant_text(session_log: &[SessionLogEntry]) -> Option<String> {
    session_log
        .iter()
        .rev()
        .map(SessionLogEntry::value)
        .find_map(|value| {
            if value.get("role").and_then(Value::as_str) != Some("assistant") {
                return None;
            }
            value
                .get("content")
                .and_then(Value::as_str)
                .map(clean_agent_message)
                .filter(|text| !text.trim().is_empty())
        })
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
    trimmed.starts_with('{')
        && (trimmed.contains("\"commands\"")
            || trimmed.contains("\"task_group\"")
            || trimmed.contains("\"step_summary\"")
            || trimmed.contains("\"tool_calls\"")
            || trimmed.contains("\"reply_message\""))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;
    use lifecycle::SessionManagement;
    use std::path::PathBuf;

    #[test]
    fn health_check_reports_role_and_version() {
        let reply = handle_envelope(&WorkerEnvelope::health_check());
        assert_eq!(reply["ok"], true);
        assert_eq!(reply["role"], "runtime_worker");
        assert_eq!(reply["version"], tura_path::instance_version());
    }

    #[test]
    fn native_tool_record_preserves_observed_status_without_inventing_attestations() {
        let observed_at = Utc::now();
        for (is_error, expected_success) in [(false, true), (true, false)] {
            let observation = NativeCodexToolObservation {
                item_id: format!("item-{is_error}"),
                item_type: "mcp_tool_call".to_string(),
                tool_name: "tura_command_graph".to_string(),
                effect_id: format!("effect-{is_error}"),
                input_sha256: "a".repeat(64),
                output_sha256: "b".repeat(64),
                status: "completed".to_string(),
                is_error,
                exit_code: None,
            };
            let record = native_tool_call_record(&observation, observed_at);

            assert_eq!(record.tool_reported_success, expected_success);
            assert!(!record.agent_reported_success);
            assert!(!record.agent_reported_helpful);
            assert!(record.agent_reported_summary.is_empty());
            assert_eq!(record.validator_reported_success, None);
            let metadata = record.provider_metadata.expect("provider metadata");
            assert_eq!(metadata["effect_id"], observation.effect_id);
            assert_eq!(metadata["item_id"], observation.item_id);
            assert_eq!(metadata["is_error"], is_error);
        }
    }

    #[test]
    fn empty_prompt_is_rejected_without_running_a_session() {
        let reply = handle_call(&json!({
            "input": {
                "input": {
                    "session_id": "s1",
                    "runtime_id": "runtime-empty-prompt",
                    "lease_id": "lease-empty-prompt"
                }
            }
        }));
        assert_eq!(reply["ok"], false);
        assert_eq!(reply["session_id"], "s1");
        assert_eq!(reply["error"], "empty prompt");
    }

    #[test]
    fn missing_session_identity_is_rejected_without_generating_a_replacement() {
        let reply = handle_call(&json!({
            "input": {
                "input": {
                    "runtime_id": "runtime-missing-session",
                    "lease_id": "lease-missing-session",
                    "prompt": "must remain attached to the caller identity"
                }
            }
        }));
        assert_eq!(reply["ok"], false);
        assert_eq!(reply["code"], "LIFECYCLE_IDENTITY_MISSING");
        assert_eq!(reply["error"], "missing session_id");
        assert!(reply.get("session_id").is_none());
    }

    #[test]
    fn task_context_capsule_requires_router_agent_spec_before_runtime_start() {
        let reply = handle_call(&json!({
            "input": {
                "input": {
                    "session_id": "task-context-missing-agent-spec",
                    "runtime_id": "runtime-task-context-missing-agent-spec",
                    "lease_id": "lease-task-context-missing-agent-spec",
                    "prompt": "must fail before provider selection",
                    "lifecycle": {
                        "transaction_id": "transaction-task-context-missing-agent-spec",
                        "commander_session_id": "commander-task-context-missing-agent-spec"
                    },
                    "jspace_contract": {"semantic_sha256": "a".repeat(64)},
                    "task_context_capsule": test_task_context_capsule()
                }
            }
        }));

        assert_eq!(reply["ok"], false);
        assert_eq!(reply["session_id"], "task-context-missing-agent-spec");
        assert_eq!(reply["code"], "TASK_CONTEXT_AGENT_SPEC_MISSING");
        assert_eq!(
            reply["error"],
            "a signed Task Context Capsule requires a router-resolved agent_spec"
        );
    }

    #[test]
    fn unknown_kind_is_reported() {
        let reply = handle_envelope(&WorkerEnvelope {
            kind: "bogus".to_string(),
            payload: Value::Null,
        });
        assert_eq!(reply["ok"], false);
        assert!(
            reply["error"]
                .as_str()
                .expect("error should be a string")
                .contains("bogus")
        );
    }

    #[test]
    fn router_parent_process_from_env_uses_pid_and_start_time() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|error| error.into_inner());
        let previous_pid = std::env::var_os("TURA_ROUTER_PARENT_PID");
        let previous_start = std::env::var_os("TURA_ROUTER_PARENT_START_TIME");
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_ROUTER_PARENT_PID", "1234")
        };
        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_ROUTER_PARENT_START_TIME", "5678")
        };

        let parent = RouterParentProcess::from_env().expect("parent env");
        assert_eq!(parent.pid, 1234);
        assert_eq!(parent.start_time, Some(5678));

        restore_env("TURA_ROUTER_PARENT_PID", previous_pid);
        restore_env("TURA_ROUTER_PARENT_START_TIME", previous_start);
    }

    #[test]
    fn current_router_parent_process_is_alive_with_current_start_time() {
        let pid = std::process::id();
        let mut system = System::new_all();
        system.refresh_processes();
        let start_time = system
            .process(Pid::from_u32(pid))
            .expect("current process should be visible")
            .start_time();

        let parent = RouterParentProcess {
            pid,
            start_time: Some(start_time),
        };
        assert!(parent.is_alive());
        let stale_parent = RouterParentProcess {
            pid,
            start_time: Some(start_time.saturating_sub(1)),
        };
        assert!(!stale_parent.is_alive());
    }

    #[test]
    fn final_assistant_text_ignores_tool_payloads() {
        let log = vec![
            SessionLogEntry::new(
                json!({"role":"assistant","content":"{\"commands\":[]}"}).to_string(),
            ),
            SessionLogEntry::new(json!({"role":"assistant","content":" done "}).to_string()),
        ];

        assert_eq!(final_assistant_text(&log), Some("done".to_string()));
    }

    #[test]
    fn failed_mano_result_preserves_provider_error_for_router_response() {
        let mut session = test_session("failed-provider-session");
        session
            .transition(SessionState::Running, Utc::now())
            .expect("running transition");
        session
            .transition(SessionState::Failed, Utc::now())
            .expect("failed transition");
        session.push_log(
            json!({"role":"assistant","content":"stale fallback"}).to_string(),
            Utc::now(),
        );

        let reply = response_from_mano_result(
            "failed-provider-session",
            ManoProcessResult {
                session,
                agents: Vec::new(),
                final_error: Some(
                    "Provider runtime failed after 3 retries before completing the task: rate_limit_exceeded"
                        .to_string(),
                ),
            },
            false,
        );

        assert_eq!(reply["ok"], false);
        assert_eq!(reply["session_id"], "failed-provider-session");
        assert_eq!(reply["session_state"], "failed");
        assert!(
            reply["error"]
                .as_str()
                .is_some_and(|error| error.contains("rate_limit_exceeded"))
        );
        assert_eq!(reply["final_text"], "stale fallback");
    }

    #[test]
    fn response_from_mano_result_includes_session_log_only_when_requested() {
        let mut session = test_session("log-response-session");
        session
            .transition(SessionState::Running, Utc::now())
            .expect("running transition");
        session.push_log(
            json!({"role":"assistant","content":"visible"}).to_string(),
            Utc::now(),
        );
        session
            .transition(SessionState::Completed, Utc::now())
            .expect("completed transition");

        let without_log = response_from_mano_result(
            "log-response-session",
            ManoProcessResult {
                session: session.clone(),
                agents: Vec::new(),
                final_error: None,
            },
            false,
        );
        let with_log = response_from_mano_result(
            "log-response-session",
            ManoProcessResult {
                session,
                agents: Vec::new(),
                final_error: None,
            },
            true,
        );

        assert!(without_log.get("session_log").is_none());
        assert_eq!(with_log["session_log"].as_array().expect("log").len(), 1);
        assert!(with_log["turn_started_at_ms"].as_i64().is_some());
    }

    fn test_session(session_id: &str) -> SessionManagement {
        SessionManagement::new(
            session_id.to_string(),
            "worker response test".to_string(),
            PathBuf::from("C:/workspace/worker-response-test"),
            false,
            "coding".to_string(),
            SessionInput {
                user_input: "test prompt".to_string(),
                file_input: Vec::new(),
                agent: None,
                runtime_context: None,
                planning_mode_override: None,
            },
            "test prompt".to_string(),
            Utc::now(),
        )
    }

    fn test_task_context_capsule() -> Value {
        let mut capsule = json!({
            "schema_version": runtime_contract::TASK_CONTEXT_CAPSULE_SCHEMA_VERSION,
            "mission": {
                "mission_id": "mission-task-context-agent-spec",
                "task_id": "task-context-agent-spec",
                "mode": "DELIVERY",
                "current_predicate": "TASK_PACKET_AND_PROVIDER_SELECTION_INTEGRITY",
                "objective": "Reject delegated context before provider fallback"
            },
            "context_summary": "Use only the router-resolved agent specification.",
            "dcf_generation": {"generation_id": "generation-task-context-agent-spec"},
            "surface": {"repo_root": "/workspace", "matched_surface_ids": ["runtime-worker"]},
            "authority": {"forbidden_effects": ["provider_fallback"]},
            "evidence_refs": [],
            "focused_verifiers": [],
            "jspace_semantic_sha256": "a".repeat(64)
        });
        capsule["semantic_sha256"] = Value::String(tura_path::jspace::semantic_sha256(&capsule));
        capsule
    }

    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn restore_env(key: &str, previous: Option<std::ffi::OsString>) {
        if let Some(value) = previous {
            // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::set_var(key, value)
            };
        } else {
            // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::remove_var(key)
            };
        }
    }
}
