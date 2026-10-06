use chrono::{DateTime, Utc};
use code_tools::command_run::CommandRunTerminalStatusGuard;
use serde_json::Value;
use std::collections::{BTreeSet, VecDeque};
use std::path::PathBuf;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
    mpsc::{self, RecvTimeoutError},
};
use std::thread::JoinHandle;
use std::time::Duration;
use tracing::error;

use crate::gateway_events::{emit_cli_live_command_run_results, emit_cli_live_command_run_started};
use crate::provider_flow::checkpointing;
use crate::provider_flow::streamed_command_run::{
    StreamedCommandEvent, StreamedCommandRunUpdate, command_run_live_delta_result,
    command_run_stream_event_command, publish_streamed_command_run_update,
    streamed_command_event_record, streamed_command_result_record,
};
use crate::router_command_run::{
    annotate_router_results_from_command, execute_command_value_results,
    resolve_router_command_bindings,
};
use crate::runtime_event_writer::RuntimeFeedPublisher;
use crate::tool_callback_sanitizer::sanitize_tool_callback_result;
use lifecycle::RuntimeProjection;
use lifecycle::{RuntimeAggregate, ToolCallRecord};

const COMMAND_RUN_TOOL_NAME: &str = "command_run";
const TERMINAL_STATUS_STREAM_ERROR: &str =
    "TERMINAL_STATUS_STREAM_INCOMPLETE: task_status done requires healthy provider completion, no remaining input or work, and no cancellation or discard";

#[derive(Clone)]
pub(crate) struct StreamedCommandRunState {
    pub(crate) results: Arc<Mutex<Vec<Value>>>,
    pub(crate) inputs: Arc<Mutex<Vec<Value>>>,
    pub(crate) events: Arc<Mutex<Vec<Value>>>,
    pub(crate) seen: Arc<AtomicBool>,
    provider_completed_healthy: Arc<AtomicBool>,
    pub(crate) cancelled: Arc<AtomicBool>,
    pub(crate) apply_patch_failed: Arc<AtomicBool>,
    pub(crate) startup_apply_patch_discarded: Arc<AtomicBool>,
    pub(crate) startup_apply_patch_discard_complete: Arc<AtomicBool>,
}

impl StreamedCommandRunState {
    pub(crate) fn new() -> Self {
        Self {
            results: Arc::new(Mutex::new(Vec::new())),
            inputs: Arc::new(Mutex::new(Vec::new())),
            events: Arc::new(Mutex::new(Vec::new())),
            seen: Arc::new(AtomicBool::new(false)),
            provider_completed_healthy: Arc::new(AtomicBool::new(false)),
            cancelled: Arc::new(AtomicBool::new(false)),
            apply_patch_failed: Arc::new(AtomicBool::new(false)),
            startup_apply_patch_discarded: Arc::new(AtomicBool::new(false)),
            startup_apply_patch_discard_complete: Arc::new(AtomicBool::new(false)),
        }
    }

    pub(crate) fn mark_seen(&self) {
        self.seen.store(true, Ordering::SeqCst);
    }

    // Only the successful provider owner, after delivering final-response commands,
    // may set this. A disconnected receiver is not evidence of healthy completion.
    pub(crate) fn mark_provider_completed_healthy(&self) {
        self.provider_completed_healthy.store(true, Ordering::SeqCst);
    }

    fn provider_completed_healthy(&self) -> bool {
        self.provider_completed_healthy.load(Ordering::SeqCst)
    }

    pub(crate) fn should_cancel_after_results(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst) && !self.snapshot_results().is_empty()
    }

    pub(crate) fn was_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst)
    }

    pub(crate) fn should_finish_after_apply_patch_failure(&self) -> bool {
        self.apply_patch_failed.load(Ordering::SeqCst) && !self.snapshot_results().is_empty()
    }

    pub(crate) fn apply_patch_failed(&self) -> bool {
        self.apply_patch_failed.load(Ordering::SeqCst)
    }

    pub(crate) fn startup_apply_patch_discarded(&self) -> bool {
        self.startup_apply_patch_discarded.load(Ordering::SeqCst)
    }

    pub(crate) fn should_finish_startup_apply_patch_discard(&self) -> bool {
        self.startup_apply_patch_discard_complete
            .load(Ordering::SeqCst)
    }

    fn mark_startup_apply_patch_discarded(&self) {
        self.startup_apply_patch_discarded
            .store(true, Ordering::SeqCst);
    }

    fn mark_startup_apply_patch_discard_complete(&self) {
        self.startup_apply_patch_discard_complete
            .store(true, Ordering::SeqCst);
    }

    fn should_stop_accepting_commands(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst)
            || self.apply_patch_failed.load(Ordering::SeqCst)
            || self.startup_apply_patch_discarded()
    }

    pub(crate) fn snapshot(&self) -> StreamedCommandRunSnapshot {
        StreamedCommandRunSnapshot {
            commands: self
                .inputs
                .lock()
                .unwrap_or_else(|err| err.into_inner())
                .clone(),
            events: self
                .events
                .lock()
                .unwrap_or_else(|err| err.into_inner())
                .clone(),
            results: self.snapshot_results(),
        }
    }

    fn snapshot_results(&self) -> Vec<Value> {
        self.results
            .lock()
            .unwrap_or_else(|err| err.into_inner())
            .clone()
    }
}

pub(crate) struct StreamedCommandRunSnapshot {
    pub(crate) commands: Vec<Value>,
    pub(crate) events: Vec<Value>,
    pub(crate) results: Vec<Value>,
}

pub(crate) struct SpawnStreamedCommandRunTask {
    pub(crate) stream_rx: mpsc::Receiver<tura_llm_rust::ProviderStreamEvent>,
    pub(crate) session_directory: PathBuf,
    pub(crate) allowed_command_run_commands: Option<BTreeSet<String>>,
    pub(crate) jspace_contract: Option<Value>,
    pub(crate) session_id: String,
    pub(crate) runtime_id: String,
    pub(crate) provider: Value,
    pub(crate) call_id: String,
    pub(crate) started_at: DateTime<Utc>,
    pub(crate) state: StreamedCommandRunState,
    pub(crate) runtime_status: RuntimeProjection,
    pub(crate) feed_publisher: Option<RuntimeFeedPublisher>,
    pub(crate) require_startup_task_state: bool,
}

#[derive(Clone)]
struct QueuedStreamCommand {
    tool_call_id: String,
    command_id: String,
    command_index: usize,
    command: Value,
    step: u64,
    declared_step: Option<u64>,
    terminal_done: bool,
    terminal_error: Option<&'static str>,
    order: usize,
}

struct StreamCommandCompletion {
    order: usize,
    completed: Vec<Value>,
    binding_results: Vec<Value>,
    halted: bool,
}

struct OrderedStreamResult {
    order: usize,
    offset: usize,
    result: Value,
}

struct StreamStepNormalizer;

impl StreamStepNormalizer {
    fn normalize(&mut self, command: &mut Value) -> u64 {
        let step = command_step(command);
        if let Some(object) = command.as_object_mut() {
            object.insert("step".to_string(), serde_json::json!(step));
        }
        step
    }
}

pub(crate) fn spawn_streamed_command_run_task(
    input: SpawnStreamedCommandRunTask,
) -> JoinHandle<Vec<Value>> {
    std::thread::spawn(move || {
        let mut results = Vec::new();
        let mut ordered_results = Vec::new();
        let mut streamed_commands = Vec::new();
        let mut command_run_started = false;
        let mut live_item_index = 0usize;
        let mut pending = VecDeque::new();
        let mut active_step = None;
        let mut running = 0usize;
        let mut receiver_open = true;
        let mut halted_before_finish = false;
        let mut next_order = 0usize;
        let mut step_normalizer = StreamStepNormalizer;
        let mut prior_stream_step = 0;
        let mut terminal_status_guard = CommandRunTerminalStatusGuard::default();
        let mut pending_done: Option<QueuedStreamCommand> = None;
        let mut output_bindings = code_tools::command_run::CommandRunOutputBindings::default();
        let mut pending_binding_results = Vec::new();
        let (completion_tx, completion_rx) = mpsc::channel::<StreamCommandCompletion>();

        loop {
            while let Ok(completion) = completion_rx.try_recv() {
                running = running.saturating_sub(1);
                if completion.binding_results.is_empty() {
                    terminal_status_guard.observe_result(None);
                }
                for result in &completion.binding_results {
                    terminal_status_guard
                        .observe_result(result.get("success").and_then(Value::as_bool));
                    terminal_status_guard.observe_step(result.get("step").and_then(Value::as_u64));
                }
                pending_binding_results.extend(completion.binding_results.iter().cloned());
                append_ordered_results(&mut ordered_results, &completion);
                emit_cli_live_command_run_results(&completion.completed, &mut live_item_index);
                record_completed_results(
                    &input.state,
                    &mut results,
                    &completion.completed,
                    &input.session_id,
                    &input.runtime_id,
                    &input.call_id,
                );
                if completion.halted {
                    halted_before_finish = true;
                    input.state.apply_patch_failed.store(true, Ordering::SeqCst);
                }
                if let Err(error) = publish_streamed_command_run_update(StreamedCommandRunUpdate {
                    session_id: &input.session_id,
                    runtime_id: &input.runtime_id,
                    provider: &input.provider,
                    call_id: &input.call_id,
                    commands: &streamed_commands,
                    results: &results,
                    status: "running",
                    started_at: input.started_at,
                    ended_at: None,
                    runtime_status: input.runtime_status.clone(),
                    publisher: input.feed_publisher.as_ref(),
                }) {
                    tracing::warn!(
                        session_id = %input.session_id,
                        runtime_id = %input.runtime_id,
                        error = %error,
                        "failed to publish streamed command completion"
                    );
                    input.state.cancelled.store(true, Ordering::SeqCst);
                }
            }

            if input.state.should_stop_accepting_commands() {
                receiver_open = false;
                pending.clear();
            }
            start_ready_stream_commands(
                &input,
                &completion_tx,
                &mut pending,
                &mut active_step,
                &mut running,
                &streamed_commands,
                &results,
                &mut output_bindings,
                &mut pending_binding_results,
            );

            if !receiver_open && running == 0 && pending.is_empty() {
                if let Some(mut done) = pending_done.take() {
                    done.terminal_error = if input.state.should_stop_accepting_commands()
                        || !input.state.provider_completed_healthy()
                    {
                        Some(TERMINAL_STATUS_STREAM_ERROR)
                    } else {
                        terminal_status_guard.done_error(done.declared_step, true)
                    };
                    publish_stream_output_bindings(
                        &mut output_bindings,
                        &mut pending_binding_results,
                    );
                    start_stream_command(
                        &input,
                        &completion_tx,
                        done,
                        &mut running,
                        &streamed_commands,
                        &results,
                        &output_bindings,
                    );
                    continue;
                }
                break;
            }

            let event = if receiver_open {
                poll_stream_event(&input.stream_rx, running == 0 && pending.is_empty())
            } else {
                StreamEventPoll::Timeout
            };
            match event {
                StreamEventPoll::Event(event) => {
                    let Some(command_event) = command_run_stream_event_command(event) else {
                        continue;
                    };
                    let mut queued = match prepare_stream_command(
                        &input,
                        command_event,
                        &mut command_run_started,
                        &mut streamed_commands,
                        &mut step_normalizer,
                        next_order,
                        &results,
                    ) {
                        Some(queued) => queued,
                        None => {
                            next_order += 1;
                            continue;
                        }
                    };
                    next_order += 1;
                    if let Some(mut done) = pending_done.take() {
                        done.terminal_error = Some(CommandRunTerminalStatusGuard::ORDER_ERROR);
                        start_stream_command(
                            &input,
                            &completion_tx,
                            done,
                            &mut running,
                            &streamed_commands,
                            &results,
                            &output_bindings,
                        );
                    }
                    let ordered_step = queued.declared_step.filter(|declared| {
                        *declared == queued.step && *declared >= prior_stream_step
                    });
                    prior_stream_step = prior_stream_step.max(queued.step);
                    if queued.terminal_done {
                        queued.declared_step = ordered_step;
                        pending_done = Some(queued);
                        continue;
                    }
                    terminal_status_guard.observe_step(ordered_step);
                    enqueue_or_start_stream_command(
                        &input,
                        &completion_tx,
                        queued,
                        &mut pending,
                        &mut active_step,
                        &mut running,
                        &streamed_commands,
                        &results,
                        &output_bindings,
                    );
                }
                StreamEventPoll::Closed => {
                    receiver_open = false;
                }
                StreamEventPoll::Timeout => {}
            }
        }
        let final_results = ordered_stream_results(ordered_results);
        let checkpoint_ack_failed = input.state.was_cancelled();
        let command_run_status = if halted_before_finish
            || checkpoint_ack_failed
            || !input.state.provider_completed_healthy()
        {
            "error"
        } else {
            "completed"
        };
        if !streamed_commands.is_empty() {
            let finished_at = Utc::now();
            if let Err(error) = publish_streamed_command_run_update(StreamedCommandRunUpdate {
                session_id: &input.session_id,
                runtime_id: &input.runtime_id,
                provider: &input.provider,
                call_id: &input.call_id,
                commands: &streamed_commands,
                results: &final_results,
                status: command_run_status,
                started_at: input.started_at,
                ended_at: Some(finished_at),
                runtime_status: input.runtime_status.clone(),
                publisher: input.feed_publisher.as_ref(),
            }) {
                tracing::warn!(
                    session_id = %input.session_id,
                    runtime_id = %input.runtime_id,
                    error = %error,
                    "failed to publish final streamed command state"
                );
                input.state.cancelled.store(true, Ordering::SeqCst);
            }
            if let Err(error) = checkpointing::command_run_finished(
                &input.session_id,
                &input.runtime_id,
                &input.call_id,
                command_run_status,
                final_results.len(),
                input.started_at,
                finished_at,
            ) {
                tracing::warn!(
                    session_id = %input.session_id,
                    runtime_id = %input.runtime_id,
                    error = %error,
                    "failed to persist command_run_finished checkpoint"
                );
            }
        }
        if halted_before_finish {
            input.state.apply_patch_failed.store(true, Ordering::SeqCst);
        }
        if input.state.startup_apply_patch_discarded() && !checkpoint_ack_failed {
            input.state.mark_startup_apply_patch_discard_complete();
        }
        final_results
    })
}

enum StreamEventPoll {
    Event(tura_llm_rust::ProviderStreamEvent),
    Timeout,
    Closed,
}

fn poll_stream_event(
    stream_rx: &mpsc::Receiver<tura_llm_rust::ProviderStreamEvent>,
    block: bool,
) -> StreamEventPoll {
    if block {
        return match stream_rx.recv() {
            Ok(event) => StreamEventPoll::Event(event),
            Err(_) => StreamEventPoll::Closed,
        };
    }
    match stream_rx.recv_timeout(Duration::from_millis(20)) {
        Ok(event) => StreamEventPoll::Event(event),
        Err(RecvTimeoutError::Timeout) => StreamEventPoll::Timeout,
        Err(RecvTimeoutError::Disconnected) => StreamEventPoll::Closed,
    }
}

fn prepare_stream_command(
    input: &SpawnStreamedCommandRunTask,
    command_event: StreamedCommandEvent,
    command_run_started: &mut bool,
    streamed_commands: &mut Vec<Value>,
    step_normalizer: &mut StreamStepNormalizer,
    order: usize,
    results: &[Value],
) -> Option<QueuedStreamCommand> {
    let StreamedCommandEvent {
        tool_call_id,
        command_index,
        command,
    } = command_event;
    if streamed_command_already_seen(streamed_commands, &tool_call_id, command_index) {
        return None;
    }
    let original_command = command;
    let (declared_step, _) = CommandRunTerminalStatusGuard::command_metadata(&original_command);
    let mut command = match code_tools::command_run::normalize_command_value_for_execution(
        original_command.clone(),
        command_index,
    ) {
        Ok(command) => command,
        Err(error) => {
            tracing::warn!(
                session_id = %input.session_id,
                runtime_id = %input.runtime_id,
                error = %error,
                "failed to normalize streamed command_run command before execution"
            );
            original_command
        }
    };
    let step = step_normalizer.normalize(&mut command);
    let (_, terminal_done) = CommandRunTerminalStatusGuard::command_metadata(&command);
    if input.require_startup_task_state && command_is_apply_patch(&command) {
        tracing::warn!(
            session_id = %input.session_id,
            runtime_id = %input.runtime_id,
            "discarding streamed apply_patch before startup task_type is effective"
        );
        input.state.mark_startup_apply_patch_discarded();
        return None;
    }
    let command_id = streamed_command_id(&input.call_id, &tool_call_id, command_index);
    attach_command_identity(
        &mut command,
        &input.call_id,
        &command_id,
        &tool_call_id,
        command_index,
    );
    if !*command_run_started {
        if let Err(error) = checkpointing::command_run_started(
            &input.session_id,
            &input.runtime_id,
            &input.call_id,
            input.started_at,
        ) {
            tracing::warn!(
                session_id = %input.session_id,
                runtime_id = %input.runtime_id,
                error = %error,
                "failed to persist command_run_started checkpoint"
            );
        }
        *command_run_started = true;
    }
    streamed_commands.push(command.clone());
    let ready_at = Utc::now();
    input
        .state
        .inputs
        .lock()
        .unwrap_or_else(|err| err.into_inner())
        .push(command.clone());
    input
        .state
        .events
        .lock()
        .unwrap_or_else(|err| err.into_inner())
        .push(streamed_command_event_record(
            "ready",
            &input.runtime_id,
            &tool_call_id,
            command_index,
            &command,
            None,
            ready_at,
        ));
    if let Err(error) = checkpointing::command_ready(
        &input.session_id,
        &input.runtime_id,
        &input.call_id,
        &command_id,
        command_index,
        &command,
        ready_at,
    ) {
        tracing::warn!(
            session_id = %input.session_id,
            runtime_id = %input.runtime_id,
            error = %error,
            "failed to persist command_ready checkpoint"
        );
    }
    if let Err(error) = publish_streamed_command_run_update(StreamedCommandRunUpdate {
        session_id: &input.session_id,
        runtime_id: &input.runtime_id,
        provider: &input.provider,
        call_id: &input.call_id,
        commands: streamed_commands,
        results,
        status: "running",
        started_at: input.started_at,
        ended_at: None,
        runtime_status: input.runtime_status.clone(),
        publisher: input.feed_publisher.as_ref(),
    }) {
        tracing::warn!(
            session_id = %input.session_id,
            runtime_id = %input.runtime_id,
            error = %error,
            "failed to publish queued streamed command"
        );
        input.state.cancelled.store(true, Ordering::SeqCst);
        return None;
    }
    Some(QueuedStreamCommand {
        tool_call_id,
        command_id,
        command_index,
        command,
        step,
        declared_step,
        terminal_done,
        terminal_error: None,
        order,
    })
}

#[expect(
    clippy::too_many_arguments,
    reason = "scheduler entrypoint passes the mutable queue state explicitly"
)]
fn enqueue_or_start_stream_command(
    input: &SpawnStreamedCommandRunTask,
    completion_tx: &mpsc::Sender<StreamCommandCompletion>,
    command: QueuedStreamCommand,
    pending: &mut VecDeque<QueuedStreamCommand>,
    active_step: &mut Option<u64>,
    running: &mut usize,
    streamed_commands: &[Value],
    results: &[Value],
    output_bindings: &code_tools::command_run::CommandRunOutputBindings,
) {
    match *active_step {
        Some(step) if command.step <= step => start_stream_command(
            input,
            completion_tx,
            command,
            running,
            streamed_commands,
            results,
            output_bindings,
        ),
        Some(_) => pending.push_back(command),
        None => {
            *active_step = Some(command.step);
            start_stream_command(
                input,
                completion_tx,
                command,
                running,
                streamed_commands,
                results,
                output_bindings,
            );
        }
    }
}

#[expect(
    clippy::too_many_arguments,
    reason = "scheduler entrypoint passes queue and output-binding state explicitly"
)]
fn start_ready_stream_commands(
    input: &SpawnStreamedCommandRunTask,
    completion_tx: &mpsc::Sender<StreamCommandCompletion>,
    pending: &mut VecDeque<QueuedStreamCommand>,
    active_step: &mut Option<u64>,
    running: &mut usize,
    streamed_commands: &[Value],
    results: &[Value],
    output_bindings: &mut code_tools::command_run::CommandRunOutputBindings,
    pending_binding_results: &mut Vec<Value>,
) {
    if *running != 0 {
        return;
    }
    let Some(next_pending_step) = pending.iter().map(|command| command.step).min() else {
        return;
    };
    match *active_step {
        Some(step) if step >= next_pending_step => {}
        _ => {
            publish_stream_output_bindings(output_bindings, pending_binding_results);
            *active_step = Some(next_pending_step);
        }
    }
    let Some(step) = *active_step else {
        return;
    };
    let mut index = 0;
    while index < pending.len() {
        if pending[index].step > step {
            index += 1;
            continue;
        }
        let command = pending
            .remove(index)
            .expect("pending index should be valid while starting ready commands");
        start_stream_command(
            input,
            completion_tx,
            command,
            running,
            streamed_commands,
            results,
            output_bindings,
        );
    }
}

fn publish_stream_output_bindings(
    output_bindings: &mut code_tools::command_run::CommandRunOutputBindings,
    pending_binding_results: &mut Vec<Value>,
) {
    for result in pending_binding_results.drain(..) {
        if result.get("success").and_then(Value::as_bool) != Some(true) {
            continue;
        }
        let Some(command_type) = result.get("command_type").and_then(Value::as_str) else {
            continue;
        };
        let Some(output) = result.get("output") else {
            continue;
        };
        output_bindings.insert(
            command_type,
            result.get("id").and_then(Value::as_str),
            output,
        );
    }
}

fn streamed_command_already_seen(
    streamed_commands: &[Value],
    tool_call_id: &str,
    command_index: usize,
) -> bool {
    streamed_commands.iter().any(|command| {
        command.get("provider_tool_call_id").and_then(Value::as_str) == Some(tool_call_id)
            && command
                .get("command_index")
                .and_then(Value::as_u64)
                .is_some_and(|index| index as usize == command_index)
    })
}

fn start_stream_command(
    input: &SpawnStreamedCommandRunTask,
    completion_tx: &mpsc::Sender<StreamCommandCompletion>,
    queued: QueuedStreamCommand,
    running: &mut usize,
    streamed_commands: &[Value],
    results: &[Value],
    output_bindings: &code_tools::command_run::CommandRunOutputBindings,
) {
    let command_started_at = Utc::now();
    emit_cli_live_command_run_started(&queued.command, &queued.tool_call_id, queued.command_index);
    if let Err(error) = checkpointing::command_started(
        &input.session_id,
        &input.runtime_id,
        &input.call_id,
        &queued.command_id,
        queued.command_index,
        &queued.command,
        command_started_at,
    ) {
        tracing::warn!(
            session_id = %input.session_id,
            runtime_id = %input.runtime_id,
            error = %error,
            "failed to persist command_started checkpoint"
        );
    }
    let live_command = queued.command.clone();
    let completion_command = live_command.clone();
    let terminal_done = queued.terminal_done;
    let mut command = queued.command;
    let resolution_error = queued
        .terminal_error
        .map(str::to_string)
        .or_else(|| resolve_router_command_bindings(&mut command, output_bindings).err())
        .or_else(|| {
            // A binding must not turn ordinary work into an unfenced terminal marker.
            (!terminal_done && CommandRunTerminalStatusGuard::command_metadata(&command).1)
                .then(|| CommandRunTerminalStatusGuard::ORDER_ERROR.to_string())
        });
    let publish_started = || {
        let mut live_results = results.to_vec();
        live_results.push(command_run_live_delta_result(
            &live_command,
            "",
            "",
            command_started_at,
        ));
        if let Err(error) = publish_streamed_command_run_update(StreamedCommandRunUpdate {
            session_id: &input.session_id,
            runtime_id: &input.runtime_id,
            provider: &input.provider,
            call_id: &input.call_id,
            commands: streamed_commands,
            results: &live_results,
            status: "running",
            started_at: input.started_at,
            ended_at: None,
            runtime_status: input.runtime_status.clone(),
            publisher: input.feed_publisher.as_ref(),
        }) {
            tracing::warn!(
                session_id = %input.session_id,
                runtime_id = %input.runtime_id,
                error = %error,
                "failed to publish streamed command start"
            );
            input.state.cancelled.store(true, Ordering::SeqCst);
        }
    };
    // Fence the terminal dispatch behind its start publication without delaying
    // ordinary same-step commands behind gateway callbacks.
    if terminal_done {
        publish_started();
    }
    let state = input.state.clone();
    let session_directory = input.session_directory.clone();
    let allowed_commands = input.allowed_command_run_commands.clone();
    let jspace_contract = input.jspace_contract.clone();
    let session_id = input.session_id.clone();
    let runtime_id = input.runtime_id.clone();
    let order = queued.order;
    let completion_tx = completion_tx.clone();
    *running += 1;
    std::thread::spawn(move || {
        let resolution_error = resolution_error.or_else(|| {
            (terminal_done
                && (state.should_stop_accepting_commands() || !state.provider_completed_healthy()))
            .then(|| TERMINAL_STATUS_STREAM_ERROR.to_string())
        });
        let mut result = if let Some(error) = resolution_error {
            crate::router_command_run::RouterCommandRunCommandResult {
                results: vec![serde_json::json!({
                    "success": false,
                    "error": error,
                })],
                halted: false,
            }
        } else {
            match tokio::runtime::Runtime::new() {
                Ok(runtime) => runtime.block_on(execute_command_value_results(
                    command,
                    session_directory,
                    Some(&session_id),
                    Some(&runtime_id),
                    allowed_commands,
                    jspace_contract,
                )),
                Err(error) => crate::router_command_run::RouterCommandRunCommandResult {
                    results: vec![serde_json::json!({
                        "success": false,
                        "error": format!("failed to create streamed command runtime: {error}"),
                    })],
                    halted: false,
                },
            }
        };
        annotate_router_results_from_command(&mut result.results, &completion_command);
        let binding_results = result.results;
        let completed = binding_results
            .iter()
            .cloned()
            .map(|mut item| {
                attach_result_identity(&mut item, &completion_command);
                sanitize_tool_callback_result(&item)
            })
            .collect();
        let _ = completion_tx.send(StreamCommandCompletion {
            order,
            completed,
            binding_results,
            halted: result.halted,
        });
    });

    if !terminal_done {
        publish_started();
    }
}

fn append_ordered_results(
    ordered_results: &mut Vec<OrderedStreamResult>,
    completion: &StreamCommandCompletion,
) {
    for (offset, result) in completion.completed.iter().cloned().enumerate() {
        ordered_results.push(OrderedStreamResult {
            order: completion.order,
            offset,
            result,
        });
    }
}

fn streamed_command_id(
    command_run_id: &str,
    provider_tool_call_id: &str,
    command_index: usize,
) -> String {
    format!("{command_run_id}:{provider_tool_call_id}:{command_index}")
}

fn attach_command_identity(
    command: &mut Value,
    command_run_id: &str,
    command_id: &str,
    provider_tool_call_id: &str,
    command_index: usize,
) {
    if let Value::Object(object) = command {
        object.insert(
            "command_run_id".to_string(),
            Value::String(command_run_id.to_string()),
        );
        object.insert(
            "command_id".to_string(),
            Value::String(command_id.to_string()),
        );
        object.insert(
            "provider_tool_call_id".to_string(),
            Value::String(provider_tool_call_id.to_string()),
        );
        object.insert(
            "command_index".to_string(),
            serde_json::json!(command_index),
        );
    }
}

fn attach_result_identity(result: &mut Value, command: &Value) {
    let Some(result_object) = result.as_object_mut() else {
        return;
    };
    for key in [
        "command_run_id",
        "command_id",
        "provider_tool_call_id",
        "command_index",
    ] {
        if !result_object.contains_key(key)
            && let Some(value) = command.get(key).cloned()
        {
            result_object.insert(key.to_string(), value);
        }
    }
    if !result_object.contains_key("command") {
        result_object.insert("command".to_string(), command.clone());
    }
}

fn ordered_stream_results(mut ordered_results: Vec<OrderedStreamResult>) -> Vec<Value> {
    ordered_results.sort_by_key(|result| (result.order, result.offset));
    ordered_results
        .into_iter()
        .map(|result| result.result)
        .collect()
}

fn command_step(command: &Value) -> u64 {
    command
        .get("step")
        .and_then(Value::as_u64)
        .unwrap_or(1)
        .max(1)
}

fn command_is_apply_patch(command: &Value) -> bool {
    command
        .get("command")
        .or_else(|| command.get("command_type"))
        .and_then(Value::as_str)
        .map(code_tools::commands::canonical_command)
        .as_deref()
        == Some("apply_patch")
}

fn record_completed_results(
    state: &StreamedCommandRunState,
    results: &mut Vec<Value>,
    completed: &[Value],
    session_id: &str,
    runtime_id: &str,
    call_id: &str,
) {
    let completed_at = Utc::now();
    if !completed.is_empty() {
        {
            let mut shared = state.results.lock().unwrap_or_else(|err| err.into_inner());
            shared.extend(completed.to_vec());
        }
    }
    for (offset, result) in completed.iter().enumerate() {
        if let Err(error) = checkpointing::streamed_command_finished(
            session_id,
            runtime_id,
            call_id,
            results.len() + offset,
            result,
            completed_at,
        ) {
            error!(
                session_id = %session_id,
                runtime_id = %runtime_id,
                error = %error,
                "session_db command checkpoint ACK failed"
            );
            state.cancelled.store(true, Ordering::SeqCst);
            break;
        }
        state
            .events
            .lock()
            .unwrap_or_else(|err| err.into_inner())
            .push(streamed_command_result_record(
                "completed",
                runtime_id,
                results.len() + offset,
                result,
                completed_at,
            ));
    }
    results.extend_from_slice(completed);
}

pub(crate) fn apply_cancelled_streamed_command_run_result(
    runtime: &mut RuntimeAggregate,
    commands: &[Value],
    events: &[Value],
    results: &[Value],
    finished_at: DateTime<Utc>,
) -> Result<(), String> {
    let mut output = streamed_command_run_output(commands, events, results);
    output["streamed_command_run_result"]["cancelled"] = Value::Bool(true);
    runtime.set_output(output)?;
    runtime.push_tool_call(streamed_command_run_tool_record(commands, finished_at))
}

pub(crate) fn apply_patch_failed_streamed_command_run_result(
    runtime: &mut RuntimeAggregate,
    commands: &[Value],
    events: &[Value],
    results: &[Value],
    finished_at: DateTime<Utc>,
) -> Result<(), String> {
    let mut output = streamed_command_run_output(commands, events, results);
    output["streamed_command_run_result"]["early_finish_reason"] =
        Value::String("apply_patch_failed".to_string());
    runtime.set_output(output)?;
    runtime.push_tool_call(streamed_command_run_tool_record(commands, finished_at))
}

pub(crate) fn apply_startup_apply_patch_discarded_streamed_command_run_result(
    runtime: &mut RuntimeAggregate,
    commands: &[Value],
    events: &[Value],
    results: &[Value],
    finished_at: DateTime<Utc>,
) -> Result<(), String> {
    runtime.set_output(streamed_command_run_output(commands, events, results))?;
    runtime.push_tool_call(streamed_command_run_tool_record(commands, finished_at))
}

pub(crate) fn ensure_streamed_command_run_tool_record(
    runtime: &mut RuntimeAggregate,
    commands: &[Value],
    finished_at: DateTime<Utc>,
) -> Result<(), String> {
    if commands.is_empty()
        || runtime
            .tool_call
            .iter()
            .any(|record| record.tool_called_name == COMMAND_RUN_TOOL_NAME)
    {
        return Ok(());
    }
    let mut record = streamed_command_run_tool_record(commands, finished_at);
    record.provider_metadata = streamed_command_run_provider_metadata(commands);
    runtime.push_tool_call(record)
}

fn streamed_command_run_output(commands: &[Value], events: &[Value], results: &[Value]) -> Value {
    let events = events
        .iter()
        .map(sanitize_tool_callback_result)
        .collect::<Vec<_>>();
    let results = results
        .iter()
        .map(sanitize_tool_callback_result)
        .collect::<Vec<_>>();
    serde_json::json!({
        "streamed_command_run_result": {
            "commands": commands,
            "command_events": events,
            "results": results,
        }
    })
}

fn streamed_command_run_tool_record(
    commands: &[Value],
    finished_at: DateTime<Utc>,
) -> ToolCallRecord {
    ToolCallRecord {
        tool_called_name: COMMAND_RUN_TOOL_NAME.to_string(),
        tool_called_input: serde_json::json!({ "commands": commands }),
        provider_metadata: None,
        tool_received_at: finished_at,
        tool_executed_at: finished_at,
        tool_calldata_received_at: finished_at,
        tool_reported_success: false,
        agent_reported_success: false,
        agent_reported_helpful: false,
        agent_reported_summary: String::new(),
        validator_reported_success: None,
    }
}

fn streamed_command_run_provider_metadata(commands: &[Value]) -> Option<Value> {
    let provider_call_id = commands
        .iter()
        .find_map(|command| {
            command
                .get("provider_tool_call_id")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
        })?
        .to_string();
    Some(serde_json::json!({
        "id": provider_call_id,
        "call_id": provider_call_id,
    }))
}

#[cfg(test)]
mod tests {
    use super::{
        SpawnStreamedCommandRunTask, StreamedCommandRunState,
        apply_patch_failed_streamed_command_run_result, ensure_streamed_command_run_tool_record,
        spawn_streamed_command_run_task, streamed_command_already_seen,
    };
    use chrono::Utc;
    use lifecycle::RuntimeState;
    use lifecycle::{ProviderConfig, ToolChoice};
    use lifecycle::{RuntimeAggregate, RuntimeProviderConfig};
    use serde_json::Value;
    use serde_json::json;
    use std::sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc,
    };
    use std::time::Duration;
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::net::TcpListener;

    static STREAMING_TEST_ENV: Mutex<()> = Mutex::new(());

    #[test]
    fn streaming_positional_range_preserves_payload_identity_and_steps() {
        let _guard = STREAMING_TEST_ENV
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        let router = MockStreamingRouter::start();
        let _router_env = EnvGuard::set("TURA_ROUTER_ADDR", &router.addr);
        let _gateway_env = EnvGuard::set("TURA_GATEWAY_CALLBACKS", "off");
        let (stream_tx, state, handle) = spawn_terminal_test_stream(false);
        stream_tx
            .send(stream_command_event("step1-a", 1, 0))
            .expect("held first step");
        router.wait_for_started(&["step1-a"], Duration::from_secs(2));

        // Ready-event consumer coverage; raw JSON accumulation belongs to the producer.
        // Whitespace and escaped quotes must survive without decoding/re-encoding the range.
        let command_line = r#" [ "src/file[1].rs", 2, 50, null, true ] "#;
        let command = json!({
            "command_type": "source_read", "command_line": command_line,
            "id": "compact-range", "label": "compact-range", "step": 2,
        });
        for _ in 0..2 {
            stream_tx
                .send(stream_command_value_event(command.clone(), 1))
                .expect("ready command and duplicate");
        }
        wait_for_stream_counts(&state, 2, 0);
        assert!(router.received("compact-range").is_none());

        router.release_step1();
        wait_for_stream_counts(&state, 2, 2);
        assert!(!state.provider_completed_healthy());
        assert!(
            !handle.is_finished(),
            "dispatch must precede stream closure"
        );
        let dispatched = router.received("compact-range").expect("range dispatched");
        assert_eq!(dispatched["command_type"], "source_read");
        assert_eq!(dispatched["command_line"].as_str(), Some(command_line));
        assert_eq!(dispatched["id"], "compact-range");
        assert_eq!(dispatched["step"], 2);

        state.mark_provider_completed_healthy();
        drop(stream_tx);
        let results = handle.join().expect("stream task");
        assert_eq!(state.snapshot().commands.len(), 2);
        assert_eq!(results.len(), 2);
        assert!(results.iter().all(|result| result["success"] == true));
        assert_eq!(results[0]["step"], 1);
        assert_eq!(results[1]["step"], 2);
        assert_eq!(results[1]["id"], "compact-range");
        assert_eq!(results[1]["command_index"], 1);
        assert_eq!(
            results[1]["command_id"],
            "stream-terminal-call:stream-tool-call:1"
        );
        assert_eq!(
            router
                .started()
                .iter()
                .filter(|label| label.as_str() == "compact-range")
                .count(),
            1
        );
    }

    #[test]
    fn streamed_command_dedupe_matches_provider_call_id_and_index() {
        let commands = vec![
            json!({
                "provider_tool_call_id": "call_1",
                "command_index": 0,
                "command_type": "shell_command",
                "command_line": "pwd"
            }),
            json!({
                "provider_tool_call_id": "call_1",
                "command_index": 1,
                "command_type": "shell_command",
                "command_line": "rg TODO"
            }),
        ];

        assert!(streamed_command_already_seen(&commands, "call_1", 0));
        assert!(streamed_command_already_seen(&commands, "call_1", 1));
        assert!(!streamed_command_already_seen(&commands, "call_1", 2));
        assert!(!streamed_command_already_seen(&commands, "call_2", 0));
    }

    fn runtime() -> RuntimeAggregate {
        RuntimeAggregate::new(
            "runtime-call-test".to_string(),
            "session-call-test".to_string(),
            "agent-call-test".to_string(),
            RuntimeProviderConfig {
                base: ProviderConfig {
                    tura_llm_name: "fast".to_string(),
                    default_model_tier: None,
                    current_model: None,
                    stream: true,
                    temperature: 0.0,
                    max_tokens: 1024,
                    tool_choice: ToolChoice::Auto,
                    time_out_ms: 30_000,
                },
                thinking: false,
                provider_name: "openai".to_string(),
                model_name: "gpt-test".to_string(),
                provider_url_name: "openai".to_string(),
                llm_provider_name: "openai".to_string(),
            },
            Utc::now(),
        )
    }

    #[test]
    fn apply_patch_failure_result_marks_early_finish_without_runtime_cancellation() {
        let mut runtime = runtime();
        let commands = vec![json!({ "command": "apply_patch failed" })];
        let events = vec![json!({ "status": "completed" })];
        let results = vec![json!({ "success": false, "error": "patch failed" })];
        let finished_at = runtime.created_at;

        apply_patch_failed_streamed_command_run_result(
            &mut runtime,
            &commands,
            &events,
            &results,
            finished_at,
        )
        .expect("apply_patch failure result should apply");

        let output = runtime.output.as_ref().expect("output should be set");
        assert_eq!(
            output.pointer("/streamed_command_run_result/early_finish_reason"),
            Some(&json!("apply_patch_failed"))
        );
        assert_eq!(
            output.pointer("/streamed_command_run_result/cancelled"),
            None
        );
        assert_eq!(runtime.tool_call.len(), 1);
        assert_eq!(runtime.tool_call[0].tool_called_name, "command_run");
        assert_eq!(
            runtime.tool_call[0].tool_called_input,
            json!({ "commands": commands })
        );
        assert_eq!(runtime.tool_call[0].tool_received_at, finished_at);
        runtime
            .mark_called(finished_at)
            .expect("runtime should enter dispatching");
        runtime
            .mark_waiting_first_token()
            .expect("runtime should wait for provider output");
        runtime
            .mark_first_token(finished_at)
            .expect("tool result should count as provider output");
        runtime
            .finish_success(finished_at, None)
            .expect("tool failure should still finish the provider runtime successfully");
        assert_eq!(runtime.state, RuntimeState::Finished);
    }

    #[test]
    fn completed_streamed_command_run_record_uses_stream_provider_call_id() {
        let mut runtime = runtime();
        let finished_at = runtime.created_at;
        let commands = vec![json!({
            "command_type": "task_status",
            "command_line": "{\"status\":\"doing\"}",
            "provider_tool_call_id": "call_streamed_command_run"
        })];

        ensure_streamed_command_run_tool_record(&mut runtime, &commands, finished_at)
            .expect("streamed tool record should apply");

        assert_eq!(runtime.tool_call.len(), 1);
        assert_eq!(runtime.tool_call[0].tool_called_name, "command_run");
        assert_eq!(
            runtime.tool_call[0].tool_called_input,
            json!({ "commands": commands })
        );
        assert_eq!(
            runtime.tool_call[0].provider_metadata,
            Some(json!({
                "id": "call_streamed_command_run",
                "call_id": "call_streamed_command_run"
            }))
        );
        assert_eq!(runtime.tool_call[0].tool_received_at, finished_at);
    }

    #[test]
    fn completed_streamed_command_run_record_does_not_duplicate_provider_record() {
        let mut runtime = runtime();
        let finished_at = runtime.created_at;
        let commands = vec![json!({ "provider_tool_call_id": "call_streamed_command_run" })];
        runtime
            .push_tool_call(lifecycle::ToolCallRecord {
                tool_called_name: "command_run".to_string(),
                tool_called_input: json!({ "commands": [] }),
                provider_metadata: Some(json!({ "id": "call_existing" })),
                tool_received_at: finished_at,
                tool_executed_at: finished_at,
                tool_calldata_received_at: finished_at,
                tool_reported_success: false,
                agent_reported_success: false,
                agent_reported_helpful: false,
                agent_reported_summary: String::new(),
                validator_reported_success: None,
            })
            .expect("existing tool record should apply");

        ensure_streamed_command_run_tool_record(&mut runtime, &commands, finished_at)
            .expect("duplicate check should succeed");

        assert_eq!(runtime.tool_call.len(), 1);
        assert_eq!(
            runtime.tool_call[0].provider_metadata,
            Some(json!({ "id": "call_existing" }))
        );
    }

    #[test]
    fn streaming_queue_runs_late_same_step_concurrently_and_waits_later_steps() {
        let _guard = STREAMING_TEST_ENV
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        let router = MockStreamingRouter::start();
        let _router_env = EnvGuard::set("TURA_ROUTER_ADDR", &router.addr);
        let _gateway_env = EnvGuard::set("TURA_GATEWAY_CALLBACKS", "off");
        let (stream_tx, stream_rx) = mpsc::channel();
        let state = StreamedCommandRunState::new();
        let handle = spawn_streamed_command_run_task(SpawnStreamedCommandRunTask {
            stream_rx,
            session_directory: std::env::temp_dir(),
            allowed_command_run_commands: None,
            jspace_contract: None,
            session_id: "stream-session".to_string(),
            runtime_id: "stream-runtime".to_string(),
            provider: json!({ "provider": "test" }),
            call_id: "stream-call".to_string(),
            started_at: Utc::now(),
            state,
            runtime_status: runtime().lifecycle_projection(),
            feed_publisher: None,
            require_startup_task_state: false,
        });

        stream_tx
            .send(stream_command_event("step1-a", 1, 0))
            .expect("first command event should send");
        router.wait_for_started(&["step1-a"], Duration::from_secs(2));

        stream_tx
            .send(stream_command_event("step1-b", 1, 1))
            .expect("second same-step command event should send");
        router.wait_for_started(&["step1-a", "step1-b"], Duration::from_secs(2));
        assert!(
            router.max_active() >= 2,
            "same-step streamed commands should reach the router concurrently"
        );

        stream_tx
            .send(stream_command_event("step2", 2, 2))
            .expect("later-step command event should send");
        std::thread::sleep(Duration::from_millis(150));
        assert!(
            !router.started().iter().any(|label| label == "step2"),
            "later-step streamed command must wait for the active step to finish"
        );

        router.release_step1();
        drop(stream_tx);
        let results = handle
            .join()
            .expect("streamed command task should not panic");

        router.wait_for_started(&["step1-a", "step1-b", "step2"], Duration::from_secs(2));
        let labels = results
            .iter()
            .map(|result| {
                result
                    .get("output")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string()
            })
            .collect::<Vec<_>>();
        assert_eq!(labels, vec!["step1-a", "step1-b", "step2"]);
        assert_eq!(results[0]["step"], 1);
        assert_eq!(results[1]["step"], 1);
        assert_eq!(results[2]["step"], 2);
    }

    #[test]
    fn streaming_queue_resolves_previous_step_output_placeholders_before_router_dispatch() {
        let _guard = STREAMING_TEST_ENV
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        let router = MockStreamingRouter::start();
        let _router_env = EnvGuard::set("TURA_ROUTER_ADDR", &router.addr);
        let _gateway_env = EnvGuard::set("TURA_GATEWAY_CALLBACKS", "off");
        let (stream_tx, stream_rx) = mpsc::channel();
        let state = StreamedCommandRunState::new();
        let handle = spawn_streamed_command_run_task(SpawnStreamedCommandRunTask {
            stream_rx,
            session_directory: std::env::temp_dir(),
            allowed_command_run_commands: None,
            jspace_contract: None,
            session_id: "stream-session-binding".to_string(),
            runtime_id: "stream-runtime-binding".to_string(),
            provider: json!({ "provider": "test" }),
            call_id: "stream-call-binding".to_string(),
            started_at: Utc::now(),
            state,
            runtime_status: runtime().lifecycle_projection(),
            feed_publisher: None,
            require_startup_task_state: false,
        });

        stream_tx
            .send(stream_binding_command_event(
                "binding-producer",
                Some("producer"),
                1,
                0,
                "{}",
            ))
            .expect("producer command event should send");
        stream_tx
            .send(stream_binding_command_event(
                "binding-consumer",
                None,
                2,
                1,
                r##"{"document_id":"#@#${producer.output.structuredContent.document_id}#@#$"}"##,
            ))
            .expect("consumer command event should send");
        drop(stream_tx);

        let results = handle
            .join()
            .expect("streamed binding command task should not panic");
        assert_eq!(results.len(), 2);
        assert!(results.iter().all(|result| result["success"] == true));
        let consumer = router
            .received("binding-consumer")
            .expect("consumer should reach the router");
        let resolved: Value = serde_json::from_str(
            consumer["command_line"]
                .as_str()
                .expect("consumer command_line"),
        )
        .expect("resolved consumer command line JSON");
        assert_eq!(resolved["document_id"], "document-17");
    }

    #[test]
    fn streaming_queue_runs_late_lower_step_with_current_active_step() {
        let _guard = STREAMING_TEST_ENV
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        let router = MockStreamingRouter::start();
        let _router_env = EnvGuard::set("TURA_ROUTER_ADDR", &router.addr);
        let _gateway_env = EnvGuard::set("TURA_GATEWAY_CALLBACKS", "off");
        let (stream_tx, stream_rx) = mpsc::channel();
        let state = StreamedCommandRunState::new();
        let handle = spawn_streamed_command_run_task(SpawnStreamedCommandRunTask {
            stream_rx,
            session_directory: std::env::temp_dir(),
            allowed_command_run_commands: None,
            jspace_contract: None,
            session_id: "stream-session-late-lower".to_string(),
            runtime_id: "stream-runtime-late-lower".to_string(),
            provider: json!({ "provider": "test" }),
            call_id: "stream-call-late-lower".to_string(),
            started_at: Utc::now(),
            state,
            runtime_status: runtime().lifecycle_projection(),
            feed_publisher: None,
            require_startup_task_state: false,
        });

        stream_tx
            .send(stream_command_event("initial-step1", 1, 0))
            .expect("initial command event should send");
        router.wait_for_started(&["initial-step1"], Duration::from_secs(2));
        std::thread::sleep(Duration::from_millis(100));

        stream_tx
            .send(stream_command_event("step2-block", 2, 1))
            .expect("current active step command event should send");
        router.wait_for_started(&["step2-block"], Duration::from_secs(2));

        stream_tx
            .send(stream_command_event("late-lower-step1", 1, 2))
            .expect("late lower step command event should send");
        router.wait_for_started(
            &["initial-step1", "step2-block", "late-lower-step1"],
            Duration::from_secs(2),
        );
        assert!(
            router.max_active() >= 2,
            "late lower step should run alongside the current active step"
        );

        stream_tx
            .send(stream_command_event("step3", 3, 3))
            .expect("future step command event should send");
        std::thread::sleep(Duration::from_millis(150));
        assert!(
            !router.started().iter().any(|label| label == "step3"),
            "future step must still wait while the current active step is running"
        );

        router.release_step2();
        drop(stream_tx);
        let results = handle
            .join()
            .expect("streamed command task should not panic");

        router.wait_for_started(
            &["initial-step1", "step2-block", "late-lower-step1", "step3"],
            Duration::from_secs(2),
        );
        let labels = results
            .iter()
            .map(|result| {
                result
                    .get("output")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string()
            })
            .collect::<Vec<_>>();
        assert_eq!(
            labels,
            vec!["initial-step1", "step2-block", "late-lower-step1", "step3"]
        );
        assert_eq!(results[0]["step"], 1);
        assert_eq!(results[1]["step"], 2);
        assert_eq!(results[2]["step"], 1);
        assert_eq!(results[3]["step"], 3);
    }

    #[test]
    fn streaming_gateway_callbacks_do_not_delay_command_start() {
        let _guard = STREAMING_TEST_ENV
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        let router = MockStreamingRouter::start();
        let _router_env = EnvGuard::set("TURA_ROUTER_ADDR", &router.addr);
        let _gateway_enabled = EnvGuard::set("TURA_GATEWAY_CALLBACKS", "1");
        let (stream_tx, stream_rx) = mpsc::channel();
        let state = StreamedCommandRunState::new();
        let handle = spawn_streamed_command_run_task(SpawnStreamedCommandRunTask {
            stream_rx,
            session_directory: std::env::temp_dir(),
            allowed_command_run_commands: None,
            jspace_contract: None,
            session_id: "stream-session-callback".to_string(),
            runtime_id: "stream-runtime-callback".to_string(),
            provider: json!({ "provider": "test" }),
            call_id: "stream-call-callback".to_string(),
            started_at: Utc::now(),
            state,
            runtime_status: runtime().lifecycle_projection(),
            feed_publisher: None,
            require_startup_task_state: false,
        });

        stream_tx
            .send(stream_command_event("callback-fast", 1, 0))
            .expect("callback test command event should send");
        router.wait_for_started(&["callback-fast"], Duration::from_millis(750));

        drop(stream_tx);
        let results = handle
            .join()
            .expect("streamed command task should not panic");
        assert_eq!(results.len(), 1);
        assert_eq!(results[0]["output"], "callback-fast");
    }

    #[test]
    fn startup_task_state_streaming_discards_apply_patch_without_cancel_failure() {
        let _guard = STREAMING_TEST_ENV
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        let router = MockStreamingRouter::start();
        let _router_env = EnvGuard::set("TURA_ROUTER_ADDR", &router.addr);
        let _gateway_env = EnvGuard::set("TURA_GATEWAY_CALLBACKS", "off");
        let (stream_tx, stream_rx) = mpsc::channel();
        let state = StreamedCommandRunState::new();
        let state_for_assert = state.clone();
        let handle = spawn_streamed_command_run_task(SpawnStreamedCommandRunTask {
            stream_rx,
            session_directory: std::env::temp_dir(),
            allowed_command_run_commands: None,
            jspace_contract: None,
            session_id: "stream-session-startup-discard".to_string(),
            runtime_id: "stream-runtime-startup-discard".to_string(),
            provider: json!({ "provider": "test" }),
            call_id: "stream-call-startup-discard".to_string(),
            started_at: Utc::now(),
            state,
            runtime_status: runtime().lifecycle_projection(),
            feed_publisher: None,
            require_startup_task_state: true,
        });

        stream_tx
            .send(stream_command_event("before-discard", 1, 0))
            .expect("first command event should send");
        router.wait_for_started(&["before-discard"], Duration::from_secs(2));
        stream_tx
            .send(stream_apply_patch_event(1))
            .expect("startup-gate apply patch event should send");
        drop(stream_tx);

        let results = handle
            .join()
            .expect("streamed command task should not panic");

        assert_eq!(results.len(), 1);
        assert_eq!(results[0]["output"], "before-discard");
        assert!(state_for_assert.startup_apply_patch_discarded());
        assert!(state_for_assert.should_finish_startup_apply_patch_discard());
        assert!(!state_for_assert.cancelled.load(Ordering::SeqCst));
        let snapshot = state_for_assert.snapshot();
        assert_eq!(snapshot.commands.len(), 1);
        assert_eq!(snapshot.commands[0]["label"], "before-discard");
    }

    #[test]
    fn streaming_terminal_done_waits_for_healthy_owner_and_settled_work() {
        let _guard = STREAMING_TEST_ENV
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        let router = MockStreamingRouter::start();
        let _router_env = EnvGuard::set("TURA_ROUTER_ADDR", &router.addr);
        let _gateway_env = EnvGuard::set("TURA_GATEWAY_CALLBACKS", "off");
        let (stream_tx, state, handle) = spawn_terminal_test_stream(false);
        for (index, label) in ["step1-a", "step1-b"].into_iter().enumerate() {
            stream_tx
                .send(stream_command_event(label, 1, index))
                .expect("same-step command");
        }
        router.wait_for_started(&["step1-a", "step1-b"], Duration::from_secs(2));
        assert!(router.max_active() >= 2);
        stream_tx
            .send(stream_binding_command_event(
                "binding-producer", Some("producer"), 2, 2, "{}",
            ))
            .expect("binding producer");
        let mut done = terminal_done_command(json!(3));
        done["command_line"] = json!({
            "status": "done",
            "task_group": concat!("#@#$", "{producer.output.structuredContent.document_id}", "#@#$")
        })
        .to_string()
        .into();
        stream_tx
            .send(stream_command_value_event(done, 3))
            .expect("final done");
        wait_for_stream_counts(&state, 4, 0);
        state.mark_provider_completed_healthy();
        assert!(router.received("terminal-done").is_none());
        router.release_step1();
        wait_for_stream_counts(&state, 4, 3);
        assert!(
            router.received("terminal-done").is_none(),
            "healthy owner alone does not close input"
        );
        assert!(!handle.is_finished());
        drop(stream_tx);
        let results = handle.join().expect("healthy terminal stream");
        assert_eq!(results.len(), 4);
        assert!(results.iter().all(|result| result["success"] == true));
        assert_eq!(
            results
                .iter()
                .map(|result| result["step"].clone())
                .collect::<Vec<_>>(),
            vec![json!(1), json!(1), json!(2), json!(3)]
        );
        assert_eq!(results[3]["id"], "terminal");
        assert_eq!(results[3]["command_index"], 3);
        assert_eq!(results[3]["command_run_id"], "stream-terminal-call");
        assert_eq!(
            results[3]["command_id"],
            "stream-terminal-call:stream-tool-call:3"
        );
        let dispatched = router.received("terminal-done").expect("final done dispatched");
        let arguments: Value =
            serde_json::from_str(dispatched["command_line"].as_str().unwrap()).unwrap();
        assert_eq!(arguments["task_group"], "document-17");
        assert_eq!(
            router
                .started()
                .iter()
                .filter(|label| label.as_str() == "terminal-done")
                .count(),
            1
        );
    }

    #[test]
    fn streaming_terminal_done_retains_failed_and_unknown_results_after_binding_drains() {
        let _guard = STREAMING_TEST_ENV
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        let router = MockStreamingRouter::start();
        let _router_env = EnvGuard::set("TURA_ROUTER_ADDR", &router.addr);
        let _gateway_env = EnvGuard::set("TURA_GATEWAY_CALLBACKS", "off");
        for (command_type, prior_results) in [
            (
                "shell_command",
                json!([{"success": false, "error": "command failed"}]),
            ),
            (
                "command_run",
                json!([{"success": false, "error": "parse failed"}]),
            ),
            ("source_read", json!([])),
            ("source_read", json!([{"output": {}}])),
            (
                "source_read",
                json!([{"success": false, "error": "source admission failed"}]),
            ),
            ("focused_verifier", json!([{"success": null}])),
            (
                "focused_verifier",
                json!([{"success": false, "error": "verification failed"}]),
            ),
        ] {
            let prior_count = prior_results.as_array().unwrap().len();
            let (stream_tx, state, handle) = spawn_terminal_test_stream(false);
            let mut prior = stream_command_value("prior-result", 1);
            prior.as_object_mut().unwrap().remove("command");
            prior["command_type"] = json!(command_type);
            prior["mock_results"] = prior_results;
            stream_tx
                .send(stream_command_value_event(prior, 0))
                .expect("prior result");
            stream_tx
                .send(stream_binding_command_event(
                    "binding-producer", Some("producer"), 2, 1, "{}",
                ))
                .expect("successful later step");
            // Advancing to step 2 drains step 1's binding results, not its failure evidence.
            wait_for_stream_counts(&state, 2, prior_count + 1);
            stream_tx
                .send(stream_command_value_event(
                    terminal_done_command(json!(3)), 2,
                ))
                .expect("done");
            state.mark_provider_completed_healthy();
            drop(stream_tx);
            let results = handle.join().expect("prior-outcome stream");
            assert_eq!(results.len(), prior_count + 2);
            assert_terminal_not_dispatched(
                &router,
                results.last().unwrap(),
                "TERMINAL_STATUS_PRIOR_RESULT",
            );
        }
    }

    #[test]
    fn streaming_terminal_done_denies_binding_failures_and_binding_created_markers() {
        let _guard = STREAMING_TEST_ENV
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        let router = MockStreamingRouter::start();
        let _router_env = EnvGuard::set("TURA_ROUTER_ADDR", &router.addr);
        let _gateway_env = EnvGuard::set("TURA_GATEWAY_CALLBACKS", "off");
        let mut missing_binding = stream_command_value("binding-missing", 1);
        missing_binding["command_line"] = json!({
            "document_id": concat!("#@#$", "{missing.output.structuredContent.document_id}", "#@#$")
        })
        .to_string()
        .into();
        let results = run_healthy_terminal_commands(vec![
            missing_binding,
            terminal_done_command(json!(2)),
        ]);
        assert_eq!(results[0]["success"], false);
        assert!(router.received("binding-missing").is_none());
        assert_terminal_not_dispatched(&router, &results[1], "TERMINAL_STATUS_PRIOR_RESULT");

        let mut producer = stream_command_value("marker-producer", 1);
        producer["id"] = json!("producer");
        producer["mock_results"] = json!([
            {"success": true, "output": {"structuredContent": {"status": "done"}}}
        ]);
        let mut marker = terminal_done_command(json!(2));
        marker["label"] = json!("binding-marker");
        marker["id"] = json!("binding-marker");
        marker["command_line"] = json!({
            "status": concat!("#@#$", "{producer.output.structuredContent.status}", "#@#$")
        })
        .to_string()
        .into();
        let results =
            run_healthy_terminal_commands(vec![producer, marker, terminal_done_command(json!(3))]);
        assert_eq!(results[1]["success"], false);
        assert!(
            results[1]["error"]
                .as_str()
                .unwrap()
                .starts_with("TERMINAL_STATUS_BATCH_ORDER")
        );
        assert!(router.received("binding-marker").is_none());
        assert_terminal_not_dispatched(&router, &results[2], "TERMINAL_STATUS_PRIOR_RESULT");
    }

    #[test]
    fn streaming_terminal_done_denies_bad_raw_steps_and_later_work() {
        let _guard = STREAMING_TEST_ENV
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        let router = MockStreamingRouter::start();
        let _router_env = EnvGuard::set("TURA_ROUTER_ADDR", &router.addr);
        let _gateway_env = EnvGuard::set("TURA_GATEWAY_CALLBACKS", "off");
        let mut missing_prior_step = stream_command_value("missing-step", 1);
        missing_prior_step.as_object_mut().unwrap().remove("step");
        let mut missing_done_step = terminal_done_command(json!(2));
        missing_done_step.as_object_mut().unwrap().remove("step");
        for commands in [
            vec![stream_command_value("prior", 1), missing_done_step],
            vec![
                stream_command_value("prior", 1),
                terminal_done_command(Value::Null),
            ],
            vec![stream_command_value("prior", 1), terminal_done_command(json!(0))],
            vec![stream_command_value("prior", 1), terminal_done_command(json!(-1))],
            vec![
                stream_command_value("prior", 1),
                terminal_done_command(json!("bad")),
            ],
            vec![stream_command_value("prior", 1), terminal_done_command(json!(1))],
            vec![missing_prior_step, terminal_done_command(json!(2))],
            vec![
                stream_command_value("prior", 2),
                stream_command_value("late-lower", 1),
                terminal_done_command(json!(3)),
            ],
            vec![
                stream_command_value("prior", 1),
                terminal_done_command(json!(2)),
                stream_command_value("later-work", 3),
            ],
            vec![
                stream_command_value("prior", 1),
                terminal_done_command(json!(2)),
                terminal_done_command(json!(3)),
            ],
        ] {
            let count = commands.len();
            let results = run_healthy_terminal_commands(commands);
            assert_eq!(results.len(), count);
            for (index, result) in results.iter().enumerate() {
                assert_eq!(result["command_index"], index);
                if result["id"] == "terminal" {
                    assert_terminal_not_dispatched(&router, result, "TERMINAL_STATUS_");
                } else {
                    assert_eq!(
                        result["success"], true,
                        "ordinary work is not serialized or discarded: {result}"
                    );
                }
            }
        }
        assert!(router.received("later-work").is_some());
    }

    #[test]
    fn streaming_terminal_done_denies_partial_cancelled_and_discarded_streams() {
        let _guard = STREAMING_TEST_ENV
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        let _gateway_env = EnvGuard::set("TURA_GATEWAY_CALLBACKS", "off");
        for gate in ["partial", "cancelled", "apply_patch_failed", "startup_discard"] {
            let router = MockStreamingRouter::start();
            let _router_env = EnvGuard::set("TURA_ROUTER_ADDR", &router.addr);
            let (stream_tx, state, handle) = spawn_terminal_test_stream(gate == "startup_discard");
            stream_tx
                .send(stream_command_event("step1-a", 1, 0))
                .expect("held prior command");
            stream_tx
                .send(stream_command_value_event(
                    terminal_done_command(json!(2)), 1,
                ))
                .expect("done");
            wait_for_stream_counts(&state, 2, 0);
            if gate != "partial" {
                state.mark_provider_completed_healthy();
            }
            match gate {
                "cancelled" => state.cancelled.store(true, Ordering::SeqCst),
                "apply_patch_failed" => state.apply_patch_failed.store(true, Ordering::SeqCst),
                "startup_discard" => stream_tx
                    .send(stream_apply_patch_event(2))
                    .expect("discarded trailing patch"),
                _ => {}
            }
            drop(stream_tx);
            router.release_step1();
            let results = handle.join().expect("unhealthy terminal stream");
            assert_eq!(results.len(), 2, "{gate}: {results:?}");
            assert_terminal_not_dispatched(
                &router,
                &results[1],
                "TERMINAL_STATUS_STREAM_INCOMPLETE",
            );
            if gate == "startup_discard" {
                assert!(state.should_finish_startup_apply_patch_discard());
                assert!(!state.was_cancelled());
            }
        }
    }

    fn spawn_terminal_test_stream(
        require_startup_task_state: bool,
    ) -> (
        mpsc::Sender<tura_llm_rust::ProviderStreamEvent>,
        StreamedCommandRunState,
        std::thread::JoinHandle<Vec<Value>>,
    ) {
        let (stream_tx, stream_rx) = mpsc::channel();
        let state = StreamedCommandRunState::new();
        let handle = spawn_streamed_command_run_task(SpawnStreamedCommandRunTask {
            stream_rx,
            session_directory: std::env::temp_dir(),
            allowed_command_run_commands: None,
            jspace_contract: None,
            session_id: "stream-terminal-session".to_string(),
            runtime_id: "stream-terminal-runtime".to_string(),
            provider: json!({"provider": "test"}),
            call_id: "stream-terminal-call".to_string(),
            started_at: Utc::now(),
            state: state.clone(),
            runtime_status: runtime().lifecycle_projection(),
            feed_publisher: None,
            require_startup_task_state,
        });
        (stream_tx, state, handle)
    }

    fn terminal_done_command(step: Value) -> Value {
        json!({
            "step": step, "label": "terminal-done", "id": "terminal",
            "command_type": "task_status", "command_line": "{\"status\":\"done\"}"
        })
    }

    fn run_healthy_terminal_commands(commands: Vec<Value>) -> Vec<Value> {
        let (stream_tx, state, handle) = spawn_terminal_test_stream(false);
        for (index, command) in commands.into_iter().enumerate() {
            stream_tx
                .send(stream_command_value_event(command, index))
                .expect("test command");
        }
        state.mark_provider_completed_healthy();
        drop(stream_tx);
        handle.join().expect("terminal stream task")
    }

    fn wait_for_stream_counts(state: &StreamedCommandRunState, commands: usize, results: usize) {
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while std::time::Instant::now() < deadline {
            let snapshot = state.snapshot();
            if snapshot.commands.len() >= commands && snapshot.results.len() >= results {
                return;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        panic!("stream did not reach {commands} commands and {results} results");
    }

    fn assert_terminal_not_dispatched(router: &MockStreamingRouter, result: &Value, prefix: &str) {
        assert_eq!(result["command_type"], "task_status", "{result}");
        assert_eq!(result["success"], false, "{result}");
        assert_eq!(result["id"], "terminal", "{result}");
        assert!(result.get("output").is_none(), "{result}");
        assert!(
            result["error"].as_str().unwrap().starts_with(prefix),
            "{result}"
        );
        assert!(
            router.received("terminal-done").is_none(),
            "done must not reach the router"
        );
    }

    fn stream_command_event(
        label: &str,
        step: u64,
        command_index: usize,
    ) -> tura_llm_rust::ProviderStreamEvent {
        stream_command_value_event(stream_command_value(label, step), command_index)
    }

    fn stream_command_value_event(
        command: Value,
        command_index: usize,
    ) -> tura_llm_rust::ProviderStreamEvent {
        tura_llm_rust::ProviderStreamEvent::CommandRunCommandReady {
            tool_call_id: "stream-tool-call".to_string(),
            command_index,
            command,
        }
    }

    fn stream_command_value(label: &str, step: u64) -> Value {
        json!({
            "step": step,
            "label": label,
            "command": "shell_command",
            "command_line": json!({
                "command": "Test-Path .",
                "timeout_ms": 5000
            }).to_string()
        })
    }

    fn stream_apply_patch_event(command_index: usize) -> tura_llm_rust::ProviderStreamEvent {
        tura_llm_rust::ProviderStreamEvent::CommandRunCommandReady {
            tool_call_id: "stream-tool-call".to_string(),
            command_index,
            command: json!({
                "step": 1,
                "command": "apply_patch",
                "command_line": "ignored patch body"
            }),
        }
    }

    fn stream_binding_command_event(
        label: &str,
        id: Option<&str>,
        step: u64,
        command_index: usize,
        command_line: &str,
    ) -> tura_llm_rust::ProviderStreamEvent {
        let mut command = json!({
            "step": step,
            "label": label,
            "command_type": "mcp_workspace",
            "command_line": command_line,
        });
        if let Some(id) = id {
            command["id"] = Value::String(id.to_string());
        }
        tura_llm_rust::ProviderStreamEvent::CommandRunCommandReady {
            tool_call_id: "stream-tool-call-binding".to_string(),
            command_index,
            command,
        }
    }

    struct MockStreamingRouter {
        addr: String,
        state: Arc<MockStreamingRouterState>,
    }

    struct MockStreamingRouterState {
        started: Mutex<Vec<String>>,
        received: Mutex<Vec<Value>>,
        release_step1: AtomicBool,
        release_step2: AtomicBool,
        active: AtomicUsize,
        max_active: AtomicUsize,
        release_notify: tokio::sync::Notify,
    }

    impl MockStreamingRouter {
        fn start() -> Self {
            let listener = std::net::TcpListener::bind("127.0.0.1:0")
                .expect("mock streaming router should bind");
            listener
                .set_nonblocking(true)
                .expect("mock streaming router should be nonblocking");
            let addr = listener
                .local_addr()
                .expect("mock streaming router should have addr")
                .to_string();
            let state = Arc::new(MockStreamingRouterState {
                started: Mutex::new(Vec::new()),
                received: Mutex::new(Vec::new()),
                release_step1: AtomicBool::new(false),
                release_step2: AtomicBool::new(false),
                active: AtomicUsize::new(0),
                max_active: AtomicUsize::new(0),
                release_notify: tokio::sync::Notify::new(),
            });
            let server_state = Arc::clone(&state);
            std::thread::spawn(move || {
                let runtime =
                    tokio::runtime::Runtime::new().expect("mock router runtime should start");
                runtime.block_on(async move {
                    let listener = TcpListener::from_std(listener)
                        .expect("mock router listener should convert to tokio");
                    while let Ok((stream, _)) = listener.accept().await {
                        let state = Arc::clone(&server_state);
                        tokio::spawn(async move {
                            let (read, mut write) = stream.into_split();
                            let mut reader = BufReader::new(read);
                            let mut line = String::new();
                            if reader.read_line(&mut line).await.unwrap_or(0) == 0 {
                                return;
                            }
                            let response = state.response_for(&line).await;
                            let _ = write.write_all(format!("{response}\n").as_bytes()).await;
                            let _ = write.flush().await;
                        });
                    }
                });
            });
            Self { addr, state }
        }

        fn started(&self) -> Vec<String> {
            self.state
                .started
                .lock()
                .unwrap_or_else(|err| err.into_inner())
                .clone()
        }

        fn wait_for_started(&self, labels: &[&str], timeout: Duration) {
            let deadline = std::time::Instant::now() + timeout;
            while std::time::Instant::now() < deadline {
                let started = self.started();
                if labels
                    .iter()
                    .all(|label| started.iter().any(|started| started == label))
                {
                    return;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            panic!(
                "timed out waiting for started labels {labels:?}; got {:?}",
                self.started()
            );
        }

        fn received(&self, label: &str) -> Option<Value> {
            self.state
                .received
                .lock()
                .unwrap_or_else(|err| err.into_inner())
                .iter()
                .find(|command| command.get("label").and_then(Value::as_str) == Some(label))
                .cloned()
        }

        fn release_step1(&self) {
            self.state.release_step1.store(true, Ordering::SeqCst);
            self.state.release_notify.notify_waiters();
        }

        fn release_step2(&self) {
            self.state.release_step2.store(true, Ordering::SeqCst);
            self.state.release_notify.notify_waiters();
        }

        fn max_active(&self) -> usize {
            self.state.max_active.load(Ordering::SeqCst)
        }
    }

    impl MockStreamingRouterState {
        async fn response_for(&self, raw: &str) -> Value {
            let request: Value =
                serde_json::from_str(raw.trim()).expect("mock router request should be JSON");
            let request_id = request
                .get("request_id")
                .and_then(Value::as_str)
                .unwrap_or("missing");
            let command = request
                .pointer("/payload/arguments/commands/0")
                .cloned()
                .unwrap_or_else(|| json!({}));
            let label = command
                .get("label")
                .and_then(Value::as_str)
                .unwrap_or("missing-label")
                .to_string();
            self.received
                .lock()
                .unwrap_or_else(|err| err.into_inner())
                .push(command.clone());
            self.record_started(label.clone());
            if label.starts_with("step1-") {
                loop {
                    let released = self.release_notify.notified();
                    if self.release_step1.load(Ordering::SeqCst) {
                        break;
                    }
                    released.await;
                }
            }
            if label == "step2-block" {
                loop {
                    let released = self.release_notify.notified();
                    if self.release_step2.load(Ordering::SeqCst) {
                        break;
                    }
                    released.await;
                }
            }
            self.active.fetch_sub(1, Ordering::SeqCst);
            let output = if label == "binding-producer" {
                json!({
                    "content": [{"type": "text", "text": "{\"document_id\":\"document-17\"}"}],
                    "structuredContent": {"document_id": "document-17"},
                    "isError": false
                })
            } else {
                Value::String(label)
            };
            let results = command.get("mock_results").cloned().unwrap_or_else(|| {
                json!([{"success": true, "output": output}])
            });
            json!({
                "request_id": request_id,
                "ok": true,
                "payload": {
                    "status": "finished",
                    "owner": "mock-router",
                    "result": {
                        "results": results
                    }
                }
            })
        }

        fn record_started(&self, label: String) {
            {
                let mut started = self.started.lock().unwrap_or_else(|err| err.into_inner());
                started.push(label);
            }
            let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
            let mut current = self.max_active.load(Ordering::SeqCst);
            while active > current {
                match self.max_active.compare_exchange(
                    current,
                    active,
                    Ordering::SeqCst,
                    Ordering::SeqCst,
                ) {
                    Ok(_) => break,
                    Err(next) => current = next,
                }
            }
        }
    }

    struct EnvGuard {
        key: &'static str,
        previous: Option<std::ffi::OsString>,
    }

    impl EnvGuard {
        fn set(key: &'static str, value: &str) -> Self {
            let previous = std::env::var_os(key);
            // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::set_var(key, value)
            };
            Self { key, previous }
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            if let Some(previous) = self.previous.take() {
                // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
                #[allow(
                    unsafe_code,
                    reason = "Rust 2024 process-environment mutation audited at the caller"
                )]
                unsafe {
                    std::env::set_var(self.key, previous)
                };
            } else {
                // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
                #[allow(
                    unsafe_code,
                    reason = "Rust 2024 process-environment mutation audited at the caller"
                )]
                unsafe {
                    std::env::remove_var(self.key)
                };
            }
        }
    }
}
