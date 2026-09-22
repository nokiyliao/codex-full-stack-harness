use serde_json::json;
use session_log_contract::{
    SessionFeedEntry, SessionFeedEvent,
    client::{SessionFeedSubscriptionCancellation, open_session_feed_subscription},
};
use std::sync::{Arc, atomic::Ordering};

use crate::app::build_state;
use crate::ipc_handlers::{
    EnqueueTurnIdentity, child_session_turn_identity, enqueue_turn_identity, handle_ipc_request,
};
use crate::process_info::{current_executable_sha256, current_process_start_time};
use crate::services::{
    execution::{DurableChildAdmissionDisposition, DurableChildCompletionState},
    recovery::recover_after_start,
    runtime_orphans::cleanup_orphan_runtime_workers,
};
use crate::shutdown::start_idle_shutdown_monitor;
use router_contract::{IpcRequest, IpcResponse, RouterEndpoint};

pub(crate) async fn serve_stdio() -> anyhow::Result<()> {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

    let _ = cleanup_orphan_runtime_workers();
    let state = build_state();
    let _ = recover_after_start(&state).await?;
    let stdin = tokio::io::stdin();
    // Shared, locked writer: each request is handled on its own task and writes
    // its response (tagged with `request_id`) when ready, so a slow call (e.g. a
    // long-running `execution.enqueue_turn`) never head-of-line blocks a
    // concurrent `health_check`. The gateway client multiplexes responses back
    // to per-call mailboxes by `request_id`.
    let stdout = Arc::new(tokio::sync::Mutex::new(tokio::io::stdout()));
    let mut lines = BufReader::new(stdin).lines();
    while let Some(line) = lines.next_line().await? {
        let trimmed = line.trim().to_string();
        if trimmed.is_empty() {
            continue;
        }
        let state = state.clone();
        let stdout = Arc::clone(&stdout);
        tokio::spawn(async move {
            let response = match serde_json::from_str::<IpcRequest>(&trimmed) {
                Ok(request) => handle_ipc_request(&state, request).await,
                Err(error) => {
                    IpcResponse::error("invalid", format!("invalid ipc request: {error}"))
                }
            };
            if let Ok(encoded) = serde_json::to_string(&response) {
                let mut out = stdout.lock().await;
                let _ = out.write_all(format!("{encoded}\n").as_bytes()).await;
                let _ = out.flush().await;
            }
        });
    }
    Ok(())
}

/// File (under the instance's db dir) recording the running router daemon's
/// socket endpoint, so any front can probe-and-connect rather than spawn its own.
pub(crate) fn router_addr_path() -> std::path::PathBuf {
    #[cfg(test)]
    if let Some(path) = ROUTER_ADDR_PATH_OVERRIDE.with(|value| value.borrow().clone()) {
        return path;
    }
    session_log_contract::client::default_db_dir().join("router.addr")
}

#[cfg(test)]
thread_local! {
    static ROUTER_ADDR_PATH_OVERRIDE: std::cell::RefCell<Option<std::path::PathBuf>> =
        const { std::cell::RefCell::new(None) };
}

#[cfg(test)]
pub(crate) fn with_router_addr_path_for_test<T>(
    path: &std::path::Path,
    operation: impl FnOnce() -> T,
) -> T {
    struct Reset(Option<std::path::PathBuf>);

    impl Drop for Reset {
        fn drop(&mut self) {
            ROUTER_ADDR_PATH_OVERRIDE.with(|value| {
                value.replace(self.0.take());
            });
        }
    }

    let previous = ROUTER_ADDR_PATH_OVERRIDE.with(|value| value.replace(Some(path.to_path_buf())));
    let _reset = Reset(previous);
    operation()
}

fn publish_router_addr(addr: &std::net::SocketAddr) -> anyhow::Result<()> {
    let path = router_addr_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let pid = std::process::id();
    let record = RouterEndpoint {
        addr: addr.to_string(),
        version: tura_path::instance_version(),
        binary_sha256: Some(current_executable_sha256()?.to_string()),
        pid: Some(pid),
        process_start_time: current_process_start_time(pid),
    };
    let tmp = path.with_extension("addr.tmp");
    std::fs::write(&tmp, serde_json::to_string(&record)?)?;
    std::fs::rename(&tmp, &path)?;
    Ok(())
}

pub(crate) fn unpublish_router_addr() {
    let pid = std::process::id();
    let process_start_time = current_process_start_time(pid);
    let _ = unpublish_router_addr_if_owned(&router_addr_path(), pid, process_start_time);
}

fn unpublish_router_addr_if_owned(
    path: &std::path::Path,
    pid: u32,
    process_start_time: Option<u64>,
) -> bool {
    let Some(process_start_time) = process_start_time else {
        return false;
    };
    let Ok(raw) = std::fs::read_to_string(path) else {
        return false;
    };
    let Ok(endpoint) = serde_json::from_str::<RouterEndpoint>(raw.trim()) else {
        return false;
    };
    if endpoint.pid != Some(pid) || endpoint.process_start_time != Some(process_start_time) {
        return false;
    }
    std::fs::remove_file(path).is_ok()
}

pub(crate) async fn serve_socket() -> anyhow::Result<()> {
    use tokio::net::TcpListener;
    use tokio::time::{Duration, timeout};

    let _router_lock = RouterDaemonLock::acquire()?;
    let orphan_report = cleanup_orphan_runtime_workers();
    if !orphan_report.killed.is_empty() {
        eprintln!(
            "router startup cleanup: killed orphan runtime workers {:?}",
            orphan_report.killed
        );
    }
    let state = build_state();
    let _ = recover_after_start(&state).await?;
    state.lifecycle.mark_activity();
    // The daemon owns the backend: bring up the single session_db owner now.
    let _ = state.session_db.start();

    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr()?;
    publish_router_addr(&addr)?;
    // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
    #[allow(
        unsafe_code,
        reason = "Rust 2024 process-environment mutation audited at the caller"
    )]
    unsafe {
        std::env::set_var("TURA_ROUTER_ADDR", addr.to_string())
    };
    eprintln!("router socket daemon listening on {addr}");
    start_idle_shutdown_monitor(state.clone());

    while !state.shutdown.load(Ordering::SeqCst) {
        let accepted = match timeout(Duration::from_millis(250), listener.accept()).await {
            Ok(accepted) => accepted?,
            Err(_) => continue,
        };
        let (stream, _) = accepted;
        let state = state.clone();
        tokio::spawn(async move {
            let _ = handle_socket_connection(state, stream).await;
        });
    }
    unpublish_router_addr();
    code_tools::shell_executor::terminate_retained_shell_process_scopes();
    Ok(())
}

async fn handle_socket_connection(
    state: crate::app::AppState,
    stream: tokio::net::TcpStream,
) -> anyhow::Result<()> {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::sync::Mutex as AsyncMutex;

    let connection_guard = ConnectionLifecycleGuard::new(state.lifecycle.clone());
    let (read, write) = stream.into_split();
    let write = Arc::new(AsyncMutex::new(write));
    let pending_tasks = Arc::new(AsyncMutex::new(Vec::<tokio::task::JoinHandle<()>>::new()));
    let mut lines = BufReader::new(read).lines();
    while let Some(line) = lines.next_line().await? {
        let trimmed = line.trim().to_string();
        if trimmed.is_empty() {
            continue;
        }
        let parsed = match serde_json::from_str::<IpcRequest>(&trimmed) {
            Ok(request) => request,
            Err(error) => {
                let response =
                    IpcResponse::error("invalid", format!("invalid ipc request: {error}"));
                if let Ok(encoded) = serde_json::to_string(&response) {
                    let mut writer = write.lock().await;
                    let _ = writer.write_all(format!("{encoded}\n").as_bytes()).await;
                    let _ = writer.flush().await;
                }
                continue;
            }
        };
        state.lifecycle.mark_activity();
        let prepared_task_packet =
            if parsed.method == router_contract::METHOD_DISPATCH_COMMANDER_TASK_PACKET {
                Some(
                    state
                        .execution
                        .prepare_commander_task_packet_dispatch(&state, parsed.payload.clone())
                        .await,
                )
            } else {
                None
            };
        let active_runtime = terminal_forwarder_identity(
            &parsed,
            prepared_task_packet
                .as_ref()
                .and_then(|prepared| prepared.as_ref().ok())
                .map(|prepared| &prepared.request),
        );
        let abort_on_disconnect = should_abort_request_on_connection_close(&parsed);
        let retain_direct_child_forwarder =
            parsed.method == router_contract::METHOD_REGISTER_CHILD_SESSION;
        let state_for_task = state.clone();
        let write_for_task = Arc::clone(&write);
        let (feed_forwarder, terminal_forwarder_error) =
            if let Some(identity) = active_runtime.as_ref() {
                match start_session_round_forwarder(
                    identity.commander_session_id.clone(),
                    identity.child_session_id.clone(),
                    identity.runtime_id.clone(),
                    identity.transaction_id.clone(),
                    state.clone(),
                    Arc::clone(&write),
                )
                .await
                {
                    Ok(forwarder) => (Some(forwarder), None),
                    Err(error) => {
                        let error = error.to_string();
                        eprintln!(
                            "router session round forwarding unavailable for {}: {error}",
                            identity.child_session_id
                        );
                        (None, Some(error))
                    }
                }
            } else {
                (None, None)
            };
        let request_id = parsed.request_id.clone();
        let handle = tokio::spawn(async move {
            let (response, task_packet_admission, pre_execution_terminal_replay) = match (
                prepared_task_packet,
                terminal_forwarder_error,
            ) {
                (Some(Ok(_)), Some(error)) => (
                    IpcResponse::error(
                        request_id.clone(),
                        format!(
                            "TASK_PACKET_PRE_ADMISSION:TASK_PACKET_TERMINAL_FORWARDER_UNAVAILABLE:{error}"
                        ),
                    ),
                    Some(DurableChildAdmissionDisposition::NotAdmitted),
                    None,
                ),
                (Some(Ok(prepared)), None) => {
                    let admission_request = prepared.request.clone();
                    let dispatch = state_for_task
                        .execution
                        .dispatch_prepared_commander_task_packet(&state_for_task, prepared)
                        .await;
                    let admission = state_for_task
                        .execution
                        .durable_child_admission_disposition(&admission_request);
                    match (dispatch, admission) {
                        (Ok(payload), Ok(DurableChildAdmissionDisposition::Admitted)) => (
                            IpcResponse::ok(request_id.clone(), payload),
                            Some(DurableChildAdmissionDisposition::Admitted),
                            None,
                        ),
                        (Err(error), Ok(DurableChildAdmissionDisposition::Admitted)) => {
                            match state_for_task
                                .execution
                                .replay_admitted_pre_execution_failure_callback(&admission_request)
                            {
                                Ok(replay) => (
                                    IpcResponse::error(request_id.clone(), error.to_string()),
                                    Some(DurableChildAdmissionDisposition::Admitted),
                                    replay,
                                ),
                                Err(replay_error) => (
                                    IpcResponse::error(
                                        request_id.clone(),
                                        format!(
                                            "{error}:ADMITTED_PRE_EXECUTION_CALLBACK_REPLAY_FAILED:{replay_error}"
                                        ),
                                    ),
                                    Some(DurableChildAdmissionDisposition::Admitted),
                                    None,
                                ),
                            }
                        }
                        (Err(error), Ok(DurableChildAdmissionDisposition::NotAdmitted)) => (
                            IpcResponse::error(request_id.clone(), error.to_string()),
                            Some(DurableChildAdmissionDisposition::NotAdmitted),
                            None,
                        ),
                        (Ok(_), Ok(DurableChildAdmissionDisposition::NotAdmitted)) => (
                            IpcResponse::error(
                                request_id.clone(),
                                format!(
                                    "TASK_PACKET_ADMISSION_READBACK_MISSING:{}",
                                    admission_request.child_session_id
                                ),
                            ),
                            Some(DurableChildAdmissionDisposition::NotAdmitted),
                            None,
                        ),
                        (_, Err(error)) => (
                            IpcResponse::error(
                                request_id.clone(),
                                format!("TASK_PACKET_ADMISSION_READBACK_FAILED:{error}"),
                            ),
                            Some(DurableChildAdmissionDisposition::NotAdmitted),
                            None,
                        ),
                    }
                }
                (Some(Err(error)), _) => (
                    IpcResponse::error(request_id.clone(), error.to_string()),
                    Some(DurableChildAdmissionDisposition::NotAdmitted),
                    None,
                ),
                (None, _) => (
                    handle_ipc_request(&state_for_task, parsed).await,
                    None,
                    None,
                ),
            };
            let replay_ready = pre_execution_terminal_replay.is_some();
            if let Some(forwarder) = feed_forwarder {
                settle_terminal_forwarder(
                    forwarder,
                    task_packet_admission,
                    retain_direct_child_forwarder,
                    response.ok,
                    replay_ready,
                )
                .await;
            };
            if let Some((callback, delivery)) = pre_execution_terminal_replay {
                match state_for_task
                    .execution
                    .continue_terminal_delivery(&state_for_task, &delivery)
                    .await
                {
                    Ok(_) => {
                        let mut writer = write_for_task.lock().await;
                        let _ = write_callback_batch(&mut *writer, vec![callback]).await;
                    }
                    Err(error) => {
                        eprintln!(
                            "router admitted pre-execution terminal delivery blocked: {error:#}"
                        );
                    }
                }
            }
            if let Ok(encoded) = serde_json::to_string(&response) {
                let mut writer = write_for_task.lock().await;
                let _ = writer.write_all(format!("{encoded}\n").as_bytes()).await;
                let _ = writer.flush().await;
            }
        });
        if abort_on_disconnect {
            pending_tasks.lock().await.push(handle);
        }
    }
    let tasks = pending_tasks.lock().await.drain(..).collect::<Vec<_>>();
    for task in tasks {
        task.abort();
    }
    drop(connection_guard);
    Ok(())
}

type SocketWriter = Arc<tokio::sync::Mutex<tokio::net::tcp::OwnedWriteHalf>>;

#[derive(Default)]
struct TerminalCallbackGate {
    callbacks: std::collections::HashMap<String, serde_json::Value>,
    deliveries:
        std::collections::HashMap<String, crate::services::execution::TerminalDeliveryIdentity>,
    completion_states: std::collections::HashMap<String, DurableChildCompletionState>,
    completed: std::collections::HashSet<String>,
}

impl TerminalCallbackGate {
    fn mark_completed(&mut self, runtime_id: String) {
        self.callbacks.remove(&runtime_id);
        self.deliveries.remove(&runtime_id);
        self.completion_states.remove(&runtime_id);
        self.completed.insert(runtime_id);
    }

    fn accept_callback(
        &mut self,
        runtime_id: String,
        callback: serde_json::Value,
    ) -> anyhow::Result<
        Option<(
            serde_json::Value,
            crate::services::execution::TerminalDeliveryIdentity,
        )>,
    > {
        if self.completed.contains(&runtime_id) {
            return Ok(None);
        }
        if let Some(existing) = self.callbacks.get(&runtime_id) {
            if existing == &callback {
                return self.take_ready(&runtime_id);
            }
            return Err(anyhow::anyhow!(
                "TERMINAL_CALLBACK_IDENTITY_CONFLICT:{runtime_id}"
            ));
        }
        self.callbacks.insert(runtime_id.clone(), callback);
        self.take_ready(&runtime_id)
    }

    fn accept_delivery(
        &mut self,
        delivery: crate::services::execution::TerminalDeliveryIdentity,
    ) -> anyhow::Result<
        Option<(
            serde_json::Value,
            crate::services::execution::TerminalDeliveryIdentity,
        )>,
    > {
        let runtime_id = delivery.runtime_id.clone();
        if self.completed.contains(&runtime_id) {
            return Ok(None);
        }
        if let Some(existing) = self.deliveries.get(&runtime_id) {
            if existing == &delivery {
                return self.take_ready(&runtime_id);
            }
            return Err(anyhow::anyhow!(
                "TERMINAL_DELIVERY_IDENTITY_CONFLICT:{runtime_id}"
            ));
        }
        self.deliveries.insert(runtime_id.clone(), delivery);
        self.take_ready(&runtime_id)
    }

    fn observe_completion(
        &mut self,
        runtime_id: String,
        completion: DurableChildCompletionState,
    ) -> anyhow::Result<
        Option<(
            serde_json::Value,
            crate::services::execution::TerminalDeliveryIdentity,
        )>,
    > {
        if self.completed.contains(&runtime_id) {
            return Ok(None);
        }
        self.completion_states
            .insert(runtime_id.clone(), completion);
        self.take_ready(&runtime_id)
    }

    fn take_ready(
        &mut self,
        runtime_id: &str,
    ) -> anyhow::Result<
        Option<(
            serde_json::Value,
            crate::services::execution::TerminalDeliveryIdentity,
        )>,
    > {
        if !matches!(
            self.completion_states.get(runtime_id),
            Some(
                DurableChildCompletionState::SimpleEmptyPlanTerminal
                    | DurableChildCompletionState::TaskManagedTerminal
            )
        ) || !self.callbacks.contains_key(runtime_id)
            || !self.deliveries.contains_key(runtime_id)
        {
            return Ok(None);
        }
        let callback = self
            .callbacks
            .remove(runtime_id)
            .ok_or_else(|| anyhow::anyhow!("TERMINAL_CALLBACK_MISSING:{runtime_id}"))?;
        let delivery = self
            .deliveries
            .remove(runtime_id)
            .ok_or_else(|| anyhow::anyhow!("TERMINAL_DELIVERY_MISSING:{runtime_id}"))?;
        self.completion_states.remove(runtime_id);
        self.completed.insert(runtime_id.to_string());
        Ok(Some((callback, delivery)))
    }
}

struct SessionRoundForwarder {
    cancellation: Option<SessionFeedSubscriptionCancellation>,
    reader: Option<tokio::task::JoinHandle<()>>,
    writer: Option<tokio::task::JoinHandle<()>>,
}

impl SessionRoundForwarder {
    async fn stop(mut self) {
        if let Some(cancellation) = self.cancellation.take() {
            let _ = cancellation.cancel();
        }
        if let Some(reader) = self.reader.take() {
            let _ = reader.await;
        }
        if let Some(writer) = self.writer.take() {
            let _ = writer.await;
        }
    }

    fn detach(mut self) {
        self.cancellation.take();
        self.reader.take();
        self.writer.take();
    }
}

impl Drop for SessionRoundForwarder {
    fn drop(&mut self) {
        if let Some(cancellation) = self.cancellation.take() {
            let _ = cancellation.cancel();
        }
    }
}

async fn settle_terminal_forwarder(
    forwarder: SessionRoundForwarder,
    task_packet_admission: Option<DurableChildAdmissionDisposition>,
    retain_direct_child_forwarder: bool,
    response_ok: bool,
    pre_execution_terminal_replay_ready: bool,
) {
    match task_packet_admission {
        Some(DurableChildAdmissionDisposition::Admitted) if pre_execution_terminal_replay_ready => {
            forwarder.stop().await
        }
        Some(DurableChildAdmissionDisposition::Admitted) => forwarder.detach(),
        Some(DurableChildAdmissionDisposition::NotAdmitted) => forwarder.stop().await,
        None if retain_direct_child_forwarder && response_ok => forwarder.detach(),
        None => forwarder.stop().await,
    }
}

async fn start_session_round_forwarder(
    commander_session_id: String,
    session_id: String,
    runtime_id: String,
    request_id: String,
    state: crate::app::AppState,
    write: SocketWriter,
) -> anyhow::Result<SessionRoundForwarder> {
    let subscription = tokio::task::spawn_blocking(open_session_feed_subscription)
        .await
        .map_err(|error| anyhow::anyhow!("session feed subscriber task failed: {error}"))??;
    let cancellation = subscription.cancellation_handle()?;
    let (sender, mut receiver) = tokio::sync::mpsc::channel(64);
    let (ready_sender, ready_receiver) = tokio::sync::oneshot::channel();
    let execution = state.execution.clone();
    let reader = tokio::task::spawn_blocking(move || {
        let mut subscription = subscription;
        let mut terminal_gate = TerminalCallbackGate::default();
        let mut replayed_terminal = false;
        let replays = match initialize_terminal_callback_replay(|| {
            execution.replay_terminal_callbacks(&commander_session_id, &session_id, &request_id)
        }) {
            Ok(replays) => replays,
            Err(error) => {
                eprintln!("router durable terminal callback replay blocked: {error}");
                let _ = ready_sender.send(Err(error));
                return;
            }
        };
        for (callback, delivery) in replays {
            replayed_terminal = true;
            terminal_gate.mark_completed(delivery.runtime_id.clone());
            if sender
                .blocking_send((vec![callback], Some(delivery)))
                .is_err()
            {
                let _ = ready_sender.send(Err(
                    "terminal callback replay queue closed before readiness".to_string(),
                ));
                return;
            }
        }
        let _ = ready_sender.send(Ok(()));
        if replayed_terminal {
            return;
        }
        while let Ok(Some(entry)) = subscription.next_entry() {
            let mut ready_terminal_callback = None;
            let terminal_callback = agent_message_is_terminal(&entry);
            let mut matched_terminal_callback = false;
            if let Some(callback) = session_round_callback(
                entry.clone(),
                &commander_session_id,
                &session_id,
                &runtime_id,
                &request_id,
            ) && let Some(runtime_id) = entry.runtime_id.as_ref()
            {
                if terminal_callback {
                    matched_terminal_callback = true;
                    match terminal_gate.accept_callback(runtime_id.clone(), callback) {
                        Ok(ready) => ready_terminal_callback = ready,
                        Err(error) => {
                            eprintln!("router terminal callback blocked: {error:#}");
                            return;
                        }
                    }
                } else if sender.blocking_send((vec![callback], None)).is_err() {
                    return;
                }
            }
            if entry.session_id == session_id {
                match execution.intake_terminal_feed_entry(&entry, &request_id) {
                    Ok(Some(delivery)) => {
                        match execution.publish_terminal_failure_callback(delivery.clone()) {
                            Ok(Some((callback, delivery))) => {
                                terminal_gate.mark_completed(delivery.runtime_id.clone());
                                if sender
                                    .blocking_send((vec![callback], Some(delivery)))
                                    .is_err()
                                {
                                    return;
                                }
                                return;
                            }
                            Ok(None) => match terminal_gate.accept_delivery(delivery) {
                                Ok(Some(ready)) => ready_terminal_callback = Some(ready),
                                Ok(None) => {}
                                Err(error) => {
                                    eprintln!("router terminal delivery blocked: {error:#}");
                                    return;
                                }
                            },
                            Err(error) => {
                                eprintln!(
                                    "router terminal failure callback publication blocked: {error:#}"
                                );
                                return;
                            }
                        }
                    }
                    Ok(None) => {}
                    Err(error) => {
                        eprintln!(
                            "router terminal receipt intake blocked for {session_id}: {error:#}"
                        );
                    }
                }
            }
            if entry.session_id == session_id || matched_terminal_callback {
                let completion = match execution.durable_admitted_child_completion(
                    &commander_session_id,
                    &session_id,
                    &runtime_id,
                    &request_id,
                ) {
                    Ok(completion) => completion,
                    Err(error) => {
                        eprintln!("router durable child completion readback blocked: {error:#}");
                        return;
                    }
                };
                match terminal_gate.observe_completion(runtime_id.clone(), completion) {
                    Ok(Some(ready)) => ready_terminal_callback = Some(ready),
                    Ok(None) => {}
                    Err(error) => {
                        eprintln!("router terminal completion blocked: {error:#}");
                        return;
                    }
                }
            }
            if let Some((callback, delivery)) = ready_terminal_callback {
                let (callback, delivery) =
                    match execution.publish_terminal_callback(delivery, callback) {
                        Ok(value) => value,
                        Err(error) => {
                            eprintln!(
                                "router durable terminal callback publication blocked: {error:#}"
                            );
                            return;
                        }
                    };
                if sender
                    .blocking_send((vec![callback], Some(delivery)))
                    .is_err()
                {
                    return;
                }
                return;
            }
        }
    });
    let continuation_execution = state.execution.clone();
    let continuation_state = state.clone();
    let writer = tokio::spawn(async move {
        while let Some((callbacks, delivery)) = receiver.recv().await {
            if let Some(delivery) = delivery
                && let Err(error) = continuation_execution
                    .continue_terminal_delivery(&continuation_state, &delivery)
                    .await
            {
                eprintln!("router terminal guidance injection blocked: {error:#}");
                break;
            }
            let write_result = {
                let mut writer = write.lock().await;
                write_callback_batch(&mut *writer, callbacks).await
            };
            if write_result.is_err() {
                break;
            }
        }
    });
    let forwarder = SessionRoundForwarder {
        cancellation: Some(cancellation),
        reader: Some(reader),
        writer: Some(writer),
    };
    match ready_receiver.await {
        Ok(Ok(())) => Ok(forwarder),
        Ok(Err(error)) => {
            forwarder.stop().await;
            Err(anyhow::anyhow!(
                "terminal callback replay initialization failed: {error}"
            ))
        }
        Err(error) => {
            forwarder.stop().await;
            Err(anyhow::anyhow!(
                "terminal callback forwarder readiness dropped: {error}"
            ))
        }
    }
}

fn initialize_terminal_callback_replay<F>(
    replay: F,
) -> Result<
    Vec<(
        serde_json::Value,
        crate::services::execution::TerminalDeliveryIdentity,
    )>,
    String,
>
where
    F: FnOnce() -> anyhow::Result<
        Vec<(
            serde_json::Value,
            crate::services::execution::TerminalDeliveryIdentity,
        )>,
    >,
{
    replay().map_err(|error| format!("{error:#}"))
}

async fn write_callback_batch<W>(
    writer: &mut W,
    callbacks: Vec<serde_json::Value>,
) -> anyhow::Result<()>
where
    W: tokio::io::AsyncWrite + Unpin,
{
    use tokio::io::AsyncWriteExt;

    for callback in callbacks {
        let encoded = serde_json::to_string(&callback)?;
        writer.write_all(format!("{encoded}\n").as_bytes()).await?;
    }
    writer.flush().await?;
    Ok(())
}

fn session_round_callback(
    entry: SessionFeedEntry,
    commander_session_id: &str,
    child_session_id: &str,
    expected_runtime_id: &str,
    request_id: &str,
) -> Option<serde_json::Value> {
    if !matches!(entry.runtime_id.as_deref(), Some(runtime_id) if runtime_id == expected_runtime_id)
        || (entry.session_id != child_session_id && entry.session_id != commander_session_id)
    {
        return None;
    }
    let SessionFeedEvent::AgentMessage {
        message_id,
        reply_message,
        runtime_status,
        context_tokens,
        usage,
        created_at,
        updated_at,
        ..
    } = entry.event
    else {
        return None;
    };
    Some(json!({
        "request_id": request_id,
        "kind": "gateway.callback",
        "method": "session.agent_message",
        "payload": {
            "session_id": child_session_id,
            "runtime_id": entry.runtime_id,
            "event_id": entry.event_id,
            "body": {
                "type": "item.completed",
                "item": {
                    "id": message_id,
                    "type": "agent_message",
                    "status": "completed",
                    "text": reply_message,
                    "runtime_status": runtime_status,
                    "context_tokens": context_tokens,
                    "usage": usage,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            }
        }
    }))
}

fn agent_message_is_terminal(entry: &SessionFeedEntry) -> bool {
    matches!(
        &entry.event,
        SessionFeedEvent::AgentMessage {
            runtime_status: Some(status),
            ..
        } if !status.live
    )
}

fn terminal_forwarder_identity(
    request: &IpcRequest,
    prepared_child: Option<&router_contract::RegisterChildSessionRequest>,
) -> Option<EnqueueTurnIdentity> {
    if request.method == router_contract::METHOD_DISPATCH_COMMANDER_TASK_PACKET {
        return prepared_child.map(child_session_turn_identity);
    }
    enqueue_turn_identity(request)
}

fn should_abort_request_on_connection_close(request: &IpcRequest) -> bool {
    !matches!(
        request.method.as_str(),
        "execution.command_run"
            | router_contract::METHOD_ENQUEUE_TURN
            | router_contract::METHOD_REGISTER_CHILD_SESSION
            | router_contract::METHOD_DISPATCH_COMMANDER_TASK_PACKET
    )
}

struct ConnectionLifecycleGuard {
    lifecycle: crate::front_lifecycle::FrontLifecycle,
}

impl ConnectionLifecycleGuard {
    fn new(lifecycle: crate::front_lifecycle::FrontLifecycle) -> Self {
        lifecycle.connection_opened();
        Self { lifecycle }
    }
}

impl Drop for ConnectionLifecycleGuard {
    fn drop(&mut self) {
        self.lifecycle.connection_closed();
    }
}

struct RouterDaemonLock {
    file: std::fs::File,
    path: std::path::PathBuf,
}

impl RouterDaemonLock {
    fn acquire() -> anyhow::Result<Self> {
        use fs2::FileExt;
        use std::io::{Seek, SeekFrom, Write};

        let dir = tura_path::locks_dir();
        std::fs::create_dir_all(&dir)?;
        let path = dir.join(format!("router-{}.lock", tura_path::build_kind()));
        let mut file = std::fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(&path)?;
        file.try_lock_exclusive().map_err(|error| {
            anyhow::anyhow!(
                "another router daemon already owns {}: {error}",
                path.display()
            )
        })?;
        file.set_len(0)?;
        file.seek(SeekFrom::Start(0))?;
        let pid = std::process::id();
        writeln!(file, "pid={pid}")?;
        writeln!(
            file,
            "process_start_time={}",
            current_process_start_time(pid).unwrap_or_default()
        )?;
        writeln!(file, "kind=router")?;
        writeln!(file, "build_kind={}", tura_path::build_kind())?;
        writeln!(file, "binary_sha256={}", current_executable_sha256()?)?;
        writeln!(file, "home={}", tura_path::instance_home().display())?;
        Ok(Self { file, path })
    }
}

impl Drop for RouterDaemonLock {
    fn drop(&mut self) {
        let _ = self.file.unlock();
        let _ = std::fs::remove_file(&self.path);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::AsyncWriteExt;

    #[test]
    fn router_endpoint_unpublish_is_exact_process_owned() -> anyhow::Result<()> {
        let root = tempfile::tempdir()?;
        let path = root.path().join("router.addr");
        let foreign = RouterEndpoint {
            addr: "127.0.0.1:1234".to_string(),
            version: tura_path::instance_version(),
            binary_sha256: Some("a".repeat(64)),
            pid: Some(42),
            process_start_time: Some(77),
        };
        let foreign_bytes = serde_json::to_vec(&foreign)?;
        std::fs::write(&path, &foreign_bytes)?;

        assert!(!unpublish_router_addr_if_owned(&path, 43, Some(77)));
        assert_eq!(std::fs::read(&path)?, foreign_bytes);
        assert!(!unpublish_router_addr_if_owned(&path, 42, Some(78)));
        assert_eq!(std::fs::read(&path)?, foreign_bytes);
        assert!(unpublish_router_addr_if_owned(&path, 42, Some(77)));
        assert!(!path.exists());
        Ok(())
    }

    #[test]
    fn durable_execution_requests_are_detached_from_runtime_socket_disconnect() {
        let request = IpcRequest {
            request_id: "command-run".to_string(),
            kind: "call".to_string(),
            method: "execution.command_run".to_string(),
            payload: json!({}),
            deadline_ms: None,
        };
        assert!(!should_abort_request_on_connection_close(&request));

        let request = IpcRequest {
            method: "execution.enqueue_turn".to_string(),
            ..request
        };
        assert!(!should_abort_request_on_connection_close(&request));

        let request = IpcRequest {
            method: router_contract::METHOD_REGISTER_CHILD_SESSION.to_string(),
            ..request
        };
        assert!(!should_abort_request_on_connection_close(&request));

        let request = IpcRequest {
            method: router_contract::METHOD_DISPATCH_COMMANDER_TASK_PACKET.to_string(),
            ..request
        };
        assert!(!should_abort_request_on_connection_close(&request));

        let request = IpcRequest {
            method: "execution.get_status".to_string(),
            ..request
        };
        assert!(should_abort_request_on_connection_close(&request));
    }

    #[test]
    fn commander_task_packet_binds_prepared_child_to_terminal_forwarder() {
        let outer = IpcRequest {
            request_id: "task-packet-dispatch".to_string(),
            kind: "call".to_string(),
            method: router_contract::METHOD_DISPATCH_COMMANDER_TASK_PACKET.to_string(),
            payload: json!({"schema_version": "tura_commander_task_packet_v1"}),
            deadline_ms: None,
        };
        assert_eq!(terminal_forwarder_identity(&outer, None), None);

        let prepared: router_contract::RegisterChildSessionRequest =
            serde_json::from_value(json!({
                "parent_session_id": "commander-1",
                "parent_mission_revision_sha256": "a".repeat(64),
                "commander_thread_id": "thread-1",
                "child_session_id": "child-1",
                "child_runtime_id": "runtime-1",
                "child_transaction_id": "transaction-1",
                "child_lease_id": "lease-1",
                "callback_request_id": "transaction-1",
                "effect_id": "runtime-1.message",
                "callback_delivery_route": "trusted_tura_direct_thread_writer",
                "delegated_input_sha256": "b".repeat(64),
                "session_directory": "/tmp/child-1",
                "session_name": "prepared child",
                "created_at_ms": 1_788_000_000_000_i64,
                "execution_payload": {"prompt": "bounded task"}
            }))
            .expect("prepared child request");

        assert_eq!(
            terminal_forwarder_identity(&outer, Some(&prepared)),
            Some(EnqueueTurnIdentity {
                commander_session_id: "commander-1".to_string(),
                child_session_id: "child-1".to_string(),
                runtime_id: "runtime-1".to_string(),
                transaction_id: "transaction-1".to_string(),
            })
        );
    }

    #[test]
    fn agent_message_feed_entry_becomes_round_callback() {
        let callback = session_round_callback(
            SessionFeedEntry {
                session_id: "session-1".to_string(),
                cursor: 7,
                runtime_id: Some("runtime-round-2".to_string()),
                event_id: "runtime-round-2:feed:3".to_string(),
                event: SessionFeedEvent::AgentMessage {
                    message_id: "runtime-round-2.message".to_string(),
                    part_id: "runtime-round-2.message".to_string(),
                    reply_message: "I found the failing boundary; next I will patch it."
                        .to_string(),
                    new_learning: String::new(),
                    runtime_status: None,
                    context_tokens: None,
                    usage: None,
                    created_at: 10,
                    updated_at: 20,
                },
            },
            "commander-1",
            "session-1",
            "runtime-round-2",
            "request-1",
        )
        .expect("matching agent message should become a callback");

        assert_eq!(callback["request_id"], "request-1");
        assert_eq!(callback["kind"], "gateway.callback");
        assert_eq!(callback["method"], "session.agent_message");
        assert_eq!(callback["payload"]["session_id"], "session-1");
        assert_eq!(callback["payload"]["runtime_id"], "runtime-round-2");
        assert_eq!(
            callback["payload"]["body"]["item"]["text"],
            "I found the failing boundary; next I will patch it."
        );
        assert_eq!(callback["payload"]["body"]["item"]["type"], "agent_message");
    }

    #[test]
    fn only_terminal_agent_message_is_held_for_durable_delivery() {
        let base = SessionFeedEntry {
            session_id: "session-1".to_string(),
            cursor: 7,
            runtime_id: Some("runtime-1".to_string()),
            event_id: "runtime-1:feed:3".to_string(),
            event: SessionFeedEvent::AgentMessage {
                message_id: "runtime-1.message".to_string(),
                part_id: "runtime-1.message".to_string(),
                reply_message: "progress".to_string(),
                new_learning: String::new(),
                runtime_status: None,
                context_tokens: None,
                usage: None,
                created_at: 10,
                updated_at: 20,
            },
        };
        assert!(!agent_message_is_terminal(&base));

        let terminal = SessionFeedEntry {
            event: SessionFeedEvent::AgentMessage {
                message_id: "runtime-1.message".to_string(),
                part_id: "runtime-1.message".to_string(),
                reply_message: "done".to_string(),
                new_learning: String::new(),
                runtime_status: Some(lifecycle::RuntimeProjection::new(
                    "runtime-1".to_string(),
                    lifecycle::RuntimeState::Finished,
                )),
                context_tokens: None,
                usage: None,
                created_at: 10,
                updated_at: 21,
            },
            ..base
        };
        assert!(agent_message_is_terminal(&terminal));
    }

    #[test]
    fn round_callback_ignores_other_sessions_and_non_message_events() {
        let entry = SessionFeedEntry {
            session_id: "session-1".to_string(),
            cursor: 1,
            runtime_id: Some("runtime-1".to_string()),
            event_id: "event-1".to_string(),
            event: SessionFeedEvent::TodosUpdated {
                todos: Vec::new(),
                updated_at: 1,
            },
        };
        assert!(
            session_round_callback(
                entry.clone(),
                "commander-1",
                "session-1",
                "runtime-1",
                "request-1",
            )
            .is_none()
        );

        let agent_entry = SessionFeedEntry {
            event: SessionFeedEvent::AgentMessage {
                message_id: "message-1".to_string(),
                part_id: "part-1".to_string(),
                reply_message: "round text".to_string(),
                new_learning: String::new(),
                runtime_status: None,
                context_tokens: None,
                usage: None,
                created_at: 1,
                updated_at: 1,
            },
            ..entry
        };
        assert!(
            session_round_callback(
                agent_entry.clone(),
                "commander-1",
                "session-2",
                "runtime-1",
                "request-1",
            )
            .is_none()
        );

        let parent_projected = SessionFeedEntry {
            session_id: "commander-1".to_string(),
            ..agent_entry.clone()
        };
        let callback = session_round_callback(
            parent_projected.clone(),
            "commander-1",
            "session-1",
            "runtime-1",
            "request-1",
        )
        .expect("exact runtime parent projection should be accepted");
        assert_eq!(callback["payload"]["session_id"], "session-1");
        assert!(
            session_round_callback(
                SessionFeedEntry {
                    runtime_id: Some("runtime-other".to_string()),
                    ..parent_projected
                },
                "commander-1",
                "session-1",
                "runtime-1",
                "request-1",
            )
            .is_none()
        );
    }

    #[test]
    fn terminal_callback_gate_empty_plan_uses_terminal_delivery_once() {
        let mut gate = TerminalCallbackGate::default();
        let callback = json!({"text": "simple child done"});
        assert!(
            gate.accept_callback("runtime-simple".to_string(), callback.clone())
                .expect("queue callback")
                .is_none()
        );
        assert!(
            gate.observe_completion(
                "runtime-simple".to_string(),
                DurableChildCompletionState::EmptyPlanPending,
            )
            .expect("observe empty plan before runtime terminal")
            .is_none()
        );
        let delivery = terminal_delivery("runtime-simple");
        assert!(
            gate.accept_delivery(delivery.clone())
                .expect("retain terminal delivery until session completion readback")
                .is_none()
        );
        let (paired_callback, paired_delivery) = gate
            .observe_completion(
                "runtime-simple".to_string(),
                DurableChildCompletionState::SimpleEmptyPlanTerminal,
            )
            .expect("observe simple empty-plan terminal state")
            .expect("simple child completion pair");
        assert_eq!(paired_callback, callback);
        assert_eq!(paired_delivery, delivery);
        assert!(
            gate.accept_delivery(delivery)
                .expect("ignore duplicate terminal delivery")
                .is_none()
        );
    }

    #[test]
    fn terminal_callback_gate_retains_callback_before_task_terminal() {
        let mut gate = TerminalCallbackGate::default();
        let runtime_id = "runtime-callback-first";
        let callback = json!({"text": "exact terminal message"});
        assert!(
            gate.accept_callback(runtime_id.to_string(), callback.clone())
                .expect("retain callback")
                .is_none()
        );
        assert!(
            gate.observe_completion(
                runtime_id.to_string(),
                DurableChildCompletionState::TaskManagedPending,
            )
            .expect("observe pending task plan")
            .is_none()
        );
        assert!(
            gate.accept_delivery(terminal_delivery(runtime_id))
                .expect("retain terminal delivery")
                .is_none(),
            "a plan-only intermediate state must not publish"
        );
        assert_eq!(gate.callbacks.get(runtime_id), Some(&callback));
        let (paired_callback, paired_delivery) = gate
            .observe_completion(
                runtime_id.to_string(),
                DurableChildCompletionState::TaskManagedTerminal,
            )
            .expect("observe task terminal")
            .expect("pair retained callback");
        assert_eq!(paired_callback, callback);
        assert_eq!(paired_delivery, terminal_delivery(runtime_id));
    }

    #[test]
    fn terminal_callback_gate_pairs_task_terminal_before_callback() {
        let mut gate = TerminalCallbackGate::default();
        let runtime_id = "runtime-task-first";
        assert!(
            gate.observe_completion(
                runtime_id.to_string(),
                DurableChildCompletionState::TaskManagedTerminal,
            )
            .expect("observe task terminal")
            .is_none()
        );
        let delivery = terminal_delivery(runtime_id);
        assert!(
            gate.accept_delivery(delivery.clone())
                .expect("queue delivery")
                .is_none()
        );
        let callback = json!({"text": "done later"});
        let (paired_callback, paired_delivery) = gate
            .accept_callback(runtime_id.to_string(), callback.clone())
            .expect("pair callback")
            .expect("ready pair");
        assert_eq!(paired_callback, callback);
        assert_eq!(paired_delivery, delivery);
    }

    #[test]
    fn terminal_callback_gate_duplicate_terminal_events_publish_once() {
        let mut gate = TerminalCallbackGate::default();
        let runtime_id = "runtime-duplicate";
        let callback = json!({"text": "done once"});
        assert!(
            gate.observe_completion(
                runtime_id.to_string(),
                DurableChildCompletionState::TaskManagedTerminal,
            )
            .expect("observe task terminal")
            .is_none()
        );
        assert!(
            gate.accept_callback(runtime_id.to_string(), callback.clone())
                .expect("queue callback")
                .is_none()
        );
        assert!(
            gate.accept_callback(runtime_id.to_string(), callback.clone())
                .expect("deduplicate callback")
                .is_none()
        );
        let delivery = terminal_delivery(runtime_id);
        let (callback, paired_delivery) = gate
            .accept_delivery(delivery.clone())
            .expect("accept delivery")
            .expect("publish once");
        assert_eq!(callback, json!({"text": "done once"}));
        assert_eq!(paired_delivery, delivery);
        assert!(
            gate.accept_delivery(terminal_delivery(runtime_id))
                .expect("deduplicate completed delivery")
                .is_none()
        );
        assert!(
            gate.accept_callback(runtime_id.to_string(), json!({"text": "done once"}))
                .expect("deduplicate completed callback")
                .is_none()
        );
    }

    #[test]
    fn terminal_callback_replay_initialization_failure_is_typed() {
        let error = initialize_terminal_callback_replay(|| {
            Err(anyhow::anyhow!("fixture replay identity conflict"))
        })
        .expect_err("replay failure must block forwarder readiness");
        assert_eq!(error, "fixture replay identity conflict");
    }

    #[tokio::test]
    async fn socket_flush_has_no_callback_or_terminal_receipt_ack_side_effect() {
        use tokio::io::AsyncReadExt;

        let (mut writer, mut reader) = tokio::io::duplex(512);
        write_callback_batch(&mut writer, vec![json!({"callback": "durable"})])
            .await
            .expect("socket write and flush");
        drop(writer);
        let mut encoded = String::new();
        reader
            .read_to_string(&mut encoded)
            .await
            .expect("read callback");
        assert_eq!(encoded, "{\"callback\":\"durable\"}\n");
    }

    #[tokio::test]
    async fn callback_write_finishes_without_a_parent_continuation_boundary() {
        use tokio::io::AsyncBufReadExt;

        let (mut writer, reader) = tokio::io::duplex(512);
        let delivery = tokio::spawn(async move {
            write_callback_batch(&mut writer, vec![json!({"callback": "durable"})]).await
        });

        let mut reader = tokio::io::BufReader::new(reader);
        let mut encoded = String::new();
        tokio::time::timeout(
            std::time::Duration::from_secs(1),
            reader.read_line(&mut encoded),
        )
        .await
        .expect("callback must not wait on parent continuation")
        .expect("read callback");
        assert_eq!(encoded, "{\"callback\":\"durable\"}\n");
        assert!(delivery.await.expect("delivery task").is_ok());
    }

    #[tokio::test]
    async fn detached_public_child_forwarder_finishes_its_terminal_tasks_once() {
        let release = std::sync::Arc::new(tokio::sync::Notify::new());
        let completed = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let reader_release = std::sync::Arc::clone(&release);
        let reader_completed = std::sync::Arc::clone(&completed);
        let reader = tokio::spawn(async move {
            reader_release.notified().await;
            reader_completed.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        });
        let writer_release = std::sync::Arc::clone(&release);
        let writer_completed = std::sync::Arc::clone(&completed);
        let writer = tokio::spawn(async move {
            writer_release.notified().await;
            writer_completed.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        });
        tokio::task::yield_now().await;

        SessionRoundForwarder {
            cancellation: None,
            reader: Some(reader),
            writer: Some(writer),
        }
        .detach();
        release.notify_waiters();

        tokio::time::timeout(std::time::Duration::from_secs(1), async {
            while completed.load(std::sync::atomic::Ordering::SeqCst) != 2 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("detached forwarder tasks must finish after terminal notification");
        assert_eq!(completed.load(std::sync::atomic::Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn durable_admission_not_response_status_controls_task_packet_forwarder_lifetime() {
        fn waiting_forwarder(
            release: &std::sync::Arc<tokio::sync::Notify>,
            completed: &std::sync::Arc<std::sync::atomic::AtomicUsize>,
        ) -> SessionRoundForwarder {
            let reader_release = std::sync::Arc::clone(release);
            let reader_completed = std::sync::Arc::clone(completed);
            let reader = tokio::spawn(async move {
                reader_release.notified().await;
                reader_completed.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            });
            let writer_release = std::sync::Arc::clone(release);
            let writer_completed = std::sync::Arc::clone(completed);
            let writer = tokio::spawn(async move {
                writer_release.notified().await;
                writer_completed.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            });
            SessionRoundForwarder {
                cancellation: None,
                reader: Some(reader),
                writer: Some(writer),
            }
        }

        let pre_admission_release = std::sync::Arc::new(tokio::sync::Notify::new());
        let pre_admission_completed = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let stopped = tokio::spawn(settle_terminal_forwarder(
            waiting_forwarder(&pre_admission_release, &pre_admission_completed),
            Some(DurableChildAdmissionDisposition::NotAdmitted),
            false,
            false,
            false,
        ));
        tokio::task::yield_now().await;
        assert!(
            !stopped.is_finished(),
            "pre-admission rejection must stop and join the forwarder"
        );
        pre_admission_release.notify_waiters();
        tokio::time::timeout(std::time::Duration::from_secs(1), stopped)
            .await
            .expect("stopped forwarder deadline")
            .expect("stopped forwarder task");
        assert_eq!(
            pre_admission_completed.load(std::sync::atomic::Ordering::SeqCst),
            2
        );

        let admitted_release = std::sync::Arc::new(tokio::sync::Notify::new());
        let admitted_completed = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let admitted_forwarder = waiting_forwarder(&admitted_release, &admitted_completed);
        tokio::task::yield_now().await;
        tokio::time::timeout(
            std::time::Duration::from_secs(1),
            settle_terminal_forwarder(
                admitted_forwarder,
                Some(DurableChildAdmissionDisposition::Admitted),
                false,
                false,
                false,
            ),
        )
        .await
        .expect("post-admission failure must detach without waiting");
        assert_eq!(
            admitted_completed.load(std::sync::atomic::Ordering::SeqCst),
            0,
            "detached terminal tasks must remain alive after an error response"
        );
        admitted_release.notify_waiters();
        tokio::time::timeout(std::time::Duration::from_secs(1), async {
            while admitted_completed.load(std::sync::atomic::Ordering::SeqCst) != 2 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("detached terminal tasks must finish exactly once");

        let terminal_release = std::sync::Arc::new(tokio::sync::Notify::new());
        let terminal_completed = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let terminal = tokio::spawn(settle_terminal_forwarder(
            waiting_forwarder(&terminal_release, &terminal_completed),
            Some(DurableChildAdmissionDisposition::Admitted),
            false,
            false,
            true,
        ));
        tokio::task::yield_now().await;
        assert!(
            !terminal.is_finished(),
            "durable pre-execution terminal replay must stop and join the live feed forwarder"
        );
        terminal_release.notify_waiters();
        tokio::time::timeout(std::time::Duration::from_secs(1), terminal)
            .await
            .expect("terminal replay forwarder deadline")
            .expect("terminal replay forwarder task");
        assert_eq!(
            terminal_completed.load(std::sync::atomic::Ordering::SeqCst),
            2
        );
    }

    fn terminal_delivery(runtime_id: &str) -> crate::services::execution::TerminalDeliveryIdentity {
        crate::services::execution::TerminalDeliveryIdentity {
            commander_session_id: "commander-1".to_string(),
            transaction_id: "transaction-1".to_string(),
            event_id: format!("{runtime_id}:terminal"),
            runtime_id: runtime_id.to_string(),
            callback_payload_sha256: None,
            callback_effect_identity: None,
        }
    }

    #[tokio::test]
    async fn command_run_survives_runtime_socket_disconnect_until_router_finishes()
    -> anyhow::Result<()> {
        let state = build_state();
        let workspace = tempfile::tempdir()?;
        let started = workspace.path().join("started.txt");
        let done = workspace.path().join("done.txt");

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let addr = listener.local_addr()?;
        let server_state = state.clone();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await?;
            let task = tokio::spawn(async move {
                let _ = handle_socket_connection(server_state, stream).await;
            });
            Ok::<_, anyhow::Error>(task)
        });

        let mut client = tokio::net::TcpStream::connect(addr).await?;
        let request = IpcRequest {
            request_id: "disconnect-command-run".to_string(),
            kind: "call".to_string(),
            method: "execution.command_run".to_string(),
            payload: json!({
                "session_id": "disconnect-session",
                "runtime_id": "disconnect-runtime",
                "session_directory": workspace.path().display().to_string(),
                "arguments": {
                    "commands": [{
                        "command": "shell_command",
                        "command_line": json!({
                            "command": disconnect_survival_command(),
                            "timeout_ms": 5000
                        }).to_string()
                    }]
                }
            }),
            deadline_ms: None,
        };
        client
            .write_all(format!("{}\n", serde_json::to_string(&request)?).as_bytes())
            .await?;
        client.flush().await?;

        wait_for_path(&started, std::time::Duration::from_secs(2)).await?;
        drop(client);

        wait_for_path(&done, std::time::Duration::from_secs(4)).await?;
        let connection_task = server.await??;
        connection_task.await?;
        wait_for_active_command_runs(&state, 0, std::time::Duration::from_secs(2)).await?;
        Ok(())
    }

    async fn wait_for_path(
        path: &std::path::Path,
        timeout: std::time::Duration,
    ) -> anyhow::Result<()> {
        let deadline = std::time::Instant::now() + timeout;
        while std::time::Instant::now() < deadline {
            if path.exists() {
                return Ok(());
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        anyhow::bail!("timed out waiting for {}", path.display())
    }

    async fn wait_for_active_command_runs(
        state: &crate::app::AppState,
        expected: usize,
        timeout: std::time::Duration,
    ) -> anyhow::Result<()> {
        let deadline = std::time::Instant::now() + timeout;
        while std::time::Instant::now() < deadline {
            if state.command_run.active_count() == expected {
                return Ok(());
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        anyhow::bail!(
            "timed out waiting for active command_run count {expected}; got {}",
            state.command_run.active_count()
        )
    }

    fn disconnect_survival_command() -> &'static str {
        if cfg!(windows) {
            "$ErrorActionPreference='Stop'; Set-Content -LiteralPath 'started.txt' -Value 'started'; Start-Sleep -Milliseconds 800; Set-Content -LiteralPath 'done.txt' -Value 'done'"
        } else {
            "set -eu; printf started > started.txt; sleep 0.8; printf done > done.txt"
        }
    }
}
