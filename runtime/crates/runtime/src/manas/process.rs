use crate::gateway_events::{
    frontend_session_id, publish_agent_message_from_runtime, publish_runtime_failure_message,
    publish_runtime_failure_message_from_runtime, publish_runtime_usage_record,
};
use crate::manas::TASK_STATUS_COMMAND;
use crate::manas::constants::PLANNING_TOOL;
use crate::manas::prompt_messages::push_no_tool_task_status_retry_message;
use crate::manas::runtime_turn::{RetryProviderInput, execute_turn};
use crate::manas::tool_catalog::{command_run_commands_for_agent, planning_child_depth};
use crate::manas::{user_visible_runtime_output_text, user_visible_runtime_text};
use crate::prompt_style::{
    provider_retry, runtime_prompt_manual, tail_injection, terminal_final_response,
};
use crate::tool_callback_sanitizer::{
    COMMAND_EVENTS_OMISSION_REASON, TerminalVerifierProof, sanitize_tool_callback_output,
};
use crate::tool_flow::execute::execute_tool_calls;
use chrono::Utc;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::thread;
use tracing::{info, warn};
use tura_llm_rust::{
    ProviderMediaFallback, provider_media_fallback, replace_unsupported_content_type_in_messages,
};

use crate::checkpoint::session_snapshot::{SessionDeltaWriter, persist_session_checkpoint};
use crate::context::{
    CompactContextAgentMessage, ContextInput, accumulate_tool_result_with_provider_metadata,
    build_context, compact_session_context_automatically_with_capabilities,
    compact_session_context_with_agent_message_and_capabilities, estimated_tokens_from_bytes_u64,
};
use crate::manas::ManasOverrides;
use crate::provider_flow::errors::{
    provider_timeout_retry_wait, runtime_failure_allows_retry,
    runtime_failure_requires_exact_input, runtime_failure_text,
};
use crate::runtime_event_writer::{RuntimeEventWriter, RuntimeFeedPublisher};
use crate::state_machine::agent_management::AgentManagement;
use crate::turn_loop::no_tool_policy::no_tool_retry_limit;
use crate::turn_loop::provider_step::accumulate_session_from_runtime;
use crate::turn_loop::retry_policy::env_flag;
use crate::turn_loop::task_progress::{
    active_doing_task_user_message, active_task_user_message, command_run_result_has_command,
    command_run_result_is_single_task_status, command_run_result_terminal_task_status,
    record_task_focus_message, record_task_focus_message_for_terminal_done,
};
use crate::turn_loop::tool_step::{command_run_results_empty, extract_compact_context_results};
use lifecycle::RuntimeAggregate;
use lifecycle::SessionManagement;
use lifecycle::{PlanStatus, RuntimeId, RuntimeState, SessionState};

const DEFAULT_MANAS_MAX_TURNS: u64 = runtime_contract::DEFAULT_MAXIMUM_RUNTIME_LLM_TURNS;

#[derive(Clone, Copy)]
enum ProviderRetryProgress {
    Wait,
    Start,
    Exhausted,
    Terminal,
}

impl ProviderRetryProgress {
    fn line(self) -> &'static [u8] {
        match self {
            Self::Wait => b"[provider] phase=retry-wait\n",
            Self::Start => b"[provider] phase=retry-start\n",
            Self::Exhausted => b"[provider] phase=retry-exhausted\n",
            Self::Terminal => b"[provider] phase=terminal\n",
        }
    }
}

fn write_provider_retry_progress(writer: &mut impl std::io::Write, phase: ProviderRetryProgress) {
    // Best-effort progress must not turn a closed stderr into a runtime failure.
    let _ = writer.write_all(phase.line());
}

fn emit_provider_retry_progress(phase: ProviderRetryProgress) {
    write_provider_retry_progress(&mut std::io::stderr().lock(), phase);
}

pub(crate) struct ManasInput<'a> {
    pub(crate) agents: &'a mut [AgentManagement],
    pub(crate) session: &'a mut SessionManagement,
    pub(crate) initial_messages: Vec<serde_json::Value>,
    pub(crate) redis_url: &'a str,
    pub(crate) initial_runtime_id: Option<RuntimeId>,
    pub(crate) initial_fallback_from_id: Option<RuntimeId>,
    pub(crate) initial_retry_provider_input: Option<RetryProviderInput>,
    pub(crate) runtime_event_writer: Option<RuntimeEventWriter>,
    pub(crate) session_delta_writer: Option<SessionDeltaWriter>,
}

pub(crate) struct ManasResult {
    pub(crate) agents: Vec<AgentManagement>,
    pub(crate) session: SessionManagement,
    pub(crate) final_error: Option<String>,
}

pub(crate) fn process_manas_internal(
    input: ManasInput,
    overrides: ManasOverrides,
) -> Result<ManasResult, String> {
    let ManasInput {
        agents,
        session,
        initial_messages,
        redis_url,
        mut initial_runtime_id,
        initial_fallback_from_id,
        mut initial_retry_provider_input,
        mut runtime_event_writer,
        mut session_delta_writer,
    } = input;
    let mut loaded_agents;
    let agents = if agents.is_empty() {
        if let Some(agent_loader) = overrides.agent_loader {
            loaded_agents = agent_loader(session)?;
            loaded_agents.as_mut_slice()
        } else {
            agents
        }
    } else {
        agents
    };

    let agent_commands = agents.first().map(command_run_commands_for_agent);
    if let Some(commands) = agent_commands.as_ref() {
        session.record_session_capabilities(commands.iter().map(String::as_str));
    }
    session.transition(SessionState::Running, Utc::now())?;
    persist_session_checkpoint(&mut session_delta_writer, session, "running")?;

    let active_agent_capabilities = agent_commands
        .as_ref()
        .map(|commands| commands.iter().cloned().collect::<Vec<_>>())
        .unwrap_or_default();
    let mut current_messages = initial_messages.clone();
    let mut last_runtime_id: Option<RuntimeId> = None;
    let mut fallback_from_id = initial_fallback_from_id;
    let original_user_task = session.input.user_input.clone();
    let mut turn = 0_u64;
    let mut provider_timeout_retries = 0_u8;
    let mut no_tool_retries = 0_u64;
    let mut final_session_state = SessionState::Completed;
    let mut final_error: Option<String> = None;
    let mut agent_marked_task_done = false;
    let mut terminal_evidence_sealed = false;
    let supports_task_status = agent_commands
        .as_ref()
        .is_some_and(|commands| commands.contains(TASK_STATUS_COMMAND));
    let supports_planning = agent_commands
        .as_ref()
        .is_some_and(|commands| commands.contains(PLANNING_TOOL));
    let evidence_only_requested =
        std::env::var("TURA_NOKIY_EVIDENCE_ONLY_TERMINAL").as_deref() == Ok("1")
            && std::env::var("TURA_NOKIY_BOUNDED_ONE_TURN").as_deref() == Ok("1")
            && agents.len() == 1
            && agents.first().is_some_and(|agent| {
                matches!(agent.agent_name.as_str(), "direct" | "balanced")
            });
    // Delivery fences remain sticky, independently of workload outcome proof.
    // A blocked handoff cannot recover failures or authorize effects/retries.
    let mut evidence_only_delivery_proven = evidence_only_requested
        && session_delta_writer.is_some()
        && runtime_event_writer.is_some()
        && !env_flag("TURA_RUNTIME_AUTO_GIT_COMMIT");
    let mut terminal_outcomes = evidence_only_delivery_proven.then(|| TerminalOutcomeTracker::new(session));
    loop {
        turn = turn.saturating_add(1);
        if turn > manas_max_turns() {
            warn!(
                session_id = %session.session_id,
                turn = turn,
                max_turns = manas_max_turns(),
                "manas turn limit reached; failing session"
            );
            let error = format!(
                "Session stopped after reaching the maximum turn limit of {}.",
                manas_max_turns()
            );
            publish_runtime_failure_message(
                session,
                last_runtime_id.as_deref().unwrap_or_default(),
                &error,
                None,
            );
            final_error = Some(error);
            final_session_state = SessionState::Failed;
            break;
        }
        info!(
            session_id = %session.session_id,
            turn = turn,
            "starting turn"
        );
        append_active_runtime_prompt_manual_context(session, &mut current_messages)?;

        let runtime_result = match execute_turn(
            agents,
            session,
            &current_messages,
            &original_user_task,
            None,
            redis_url,
            turn == 1,
            false,
            false,
            initial_runtime_id.take(),
            fallback_from_id.take(),
            initial_retry_provider_input.take(),
            runtime_event_writer.as_mut(),
        ) {
            Ok(result) => result,
            Err(error) => {
                warn!(
                    session_id = %session.session_id,
                    turn = turn,
                    error = %error,
                    "runtime failed during turn; publishing visible fallback"
                );
                let runtime_id = last_runtime_id
                    .unwrap_or_else(|| format!("runtime-error-{}", session.session_id));
                publish_runtime_failure_message(session, &runtime_id, &error, None);
                if env_flag("TURA_RUNTIME_ERRORS_FATAL") {
                    return Err(error);
                }
                final_error = Some(error);
                final_session_state = SessionState::Failed;
                break;
            }
        };

        let runtime = runtime_result.0;
        let tool_calls = runtime_result.1;
        evidence_only_delivery_proven &= runtime.state == RuntimeState::Finished
            && runtime.session_id == session.session_id
            && runtime.tool_call.len() == tool_calls.len();

        last_runtime_id = Some(runtime.runtime_id.clone());

        let feed_publisher = runtime_feed_publisher(&mut runtime_event_writer, &runtime)?;

        if let Err(error) = accumulate_session_from_runtime(session, &runtime, true) {
            seal_runtime_feed(&mut runtime_event_writer, &runtime.runtime_id)?;
            return Err(error);
        }
        increment_turn_with_fresh_timestamp(session);
        if let Err(error) =
            persist_session_checkpoint(&mut session_delta_writer, session, "runtime")
        {
            seal_runtime_feed(&mut runtime_event_writer, &runtime.runtime_id)?;
            return Err(error);
        }
        publish_runtime_usage_record(session, &runtime, feed_publisher.as_ref());

        if runtime.state == RuntimeState::TimedOut || runtime_failure_allows_retry(&runtime) {
            let error_text = runtime_failure_text(&runtime)
                .unwrap_or_else(|| "Provider runtime failed before producing output.".to_string());
            if let Some(wait_duration) = provider_timeout_retry_wait(provider_timeout_retries) {
                if let Some(ProviderMediaFallback::UnsupportedRequiredContent { content_type }) =
                    provider_media_fallback(&error_text)
                {
                    warn!(
                        session_id = %session.session_id,
                        turn = turn,
                        runtime_id = %runtime.runtime_id,
                        content_type = content_type,
                        error = %error_text,
                        "provider rejected required media content; not retrying without media"
                    );
                    let error = format!(
                        "Provider/model does not support `{content_type}` media input for this request. Use an image-capable model or a route whose model metadata includes that input modality. Original provider error: {error_text}"
                    );
                    emit_provider_retry_progress(ProviderRetryProgress::Terminal);
                    publish_runtime_failure_message_from_runtime(
                        session,
                        &runtime,
                        &error,
                        feed_publisher.as_ref(),
                    );
                    seal_runtime_feed(&mut runtime_event_writer, &runtime.runtime_id)?;
                    final_error = Some(error);
                    final_session_state = SessionState::Failed;
                    break;
                }
                let exact_input_retry = runtime_failure_requires_exact_input(&runtime);
                let removed_media = (!exact_input_retry)
                    .then(|| {
                        provider_media_fallback(&error_text)
                            .and_then(ProviderMediaFallback::retry_content_type)
                            .map(|content_type| {
                                let removed = replace_unsupported_content_type_in_messages(
                                    &mut current_messages,
                                    content_type,
                                );
                                (content_type, removed)
                            })
                            .filter(|(_, removed)| *removed > 0)
                    })
                    .flatten();
                provider_timeout_retries = provider_timeout_retries.saturating_add(1);
                warn!(
                    session_id = %session.session_id,
                    turn = turn,
                    runtime_id = %runtime.runtime_id,
                    status = ?runtime.call_result_status(),
                    error = %error_text,
                    retry = provider_timeout_retries,
                    wait_ms = wait_duration.as_millis(),
                    "provider runtime failed transiently; waiting before retrying with full tool set"
                );
                emit_provider_retry_progress(ProviderRetryProgress::Wait);
                thread::sleep(wait_duration);
                if let Some((content_type, removed)) = removed_media {
                    tail_injection::append_tail_prompt(
                        &mut current_messages,
                        tail_injection::TailPrompt::developer(provider_retry::media_fallback(
                            content_type,
                            removed,
                        )),
                    );
                }
                if !exact_input_retry {
                    tail_injection::append_tail_prompt(
                        &mut current_messages,
                        tail_injection::TailPrompt::developer(
                            provider_retry::transient_failure_retry(
                                &error_text,
                                provider_timeout_retries,
                                3,
                            ),
                        ),
                    );
                }
                fallback_from_id = Some(runtime.runtime_id.clone());
                seal_runtime_feed(&mut runtime_event_writer, &runtime.runtime_id)?;
                emit_provider_retry_progress(ProviderRetryProgress::Start);
                continue;
            }

            warn!(
                session_id = %session.session_id,
                turn = turn,
                runtime_id = %runtime.runtime_id,
                status = ?runtime.call_result_status(),
                error = %error_text,
                retries = provider_timeout_retries,
                "provider runtime failed transiently after retries; publishing visible failure"
            );
            emit_provider_retry_progress(ProviderRetryProgress::Exhausted);
            let error = format!(
                "Provider runtime failed after 3 retries before completing the task: {error_text}"
            );
            publish_runtime_failure_message_from_runtime(
                session,
                &runtime,
                &error,
                feed_publisher.as_ref(),
            );
            seal_runtime_feed(&mut runtime_event_writer, &runtime.runtime_id)?;
            final_error = Some(error);
            final_session_state = SessionState::Failed;
            break;
        }
        if runtime.state == RuntimeState::Failed {
            let error_text = runtime_failure_text(&runtime)
                .unwrap_or_else(|| "Provider runtime failed before producing output.".to_string());
            warn!(
                session_id = %session.session_id,
                turn = turn,
                runtime_id = %runtime.runtime_id,
                error = %error_text,
                "provider runtime failed"
            );
            emit_provider_retry_progress(ProviderRetryProgress::Terminal);
            publish_runtime_failure_message_from_runtime(
                session,
                &runtime,
                &error_text,
                feed_publisher.as_ref(),
            );
            seal_runtime_feed(&mut runtime_event_writer, &runtime.runtime_id)?;
            if env_flag("TURA_RUNTIME_ERRORS_FATAL") {
                return Err(error_text);
            }
            final_error = Some(error_text);
            final_session_state = SessionState::Failed;
            break;
        }

        if !tool_calls.is_empty() {
            let visible_reply_before_tool = visible_runtime_reply(&runtime);
            let visible_reply_published_before_terminal_status =
                if let Some(content) = visible_reply_before_tool.as_deref() {
                    match publish_agent_message_from_runtime(
                        &session.session_id,
                        &runtime,
                        content.to_string(),
                        String::new(),
                        feed_publisher.as_ref(),
                    ) {
                        Ok(_) => true,
                        Err(error) => {
                            warn!(
                                session_id = %session.session_id,
                                runtime_id = %runtime.runtime_id,
                                error = %error,
                                "failed to publish assistant text before tool execution"
                            );
                            false
                        }
                    }
                } else {
                    false
                };
            evidence_only_delivery_proven &= visible_reply_before_tool.is_none()
                || visible_reply_published_before_terminal_status;
            provider_timeout_retries = 0;
            no_tool_retries = 0;
            let tool_results = execute_tool_calls(
                &tool_calls,
                agents.first(),
                session,
                &runtime,
                redis_url,
                feed_publisher.as_ref(),
            );
            seal_runtime_feed(&mut runtime_event_writer, &runtime.runtime_id)?;
            let mut tool_results = tool_results?;
            evidence_only_delivery_proven &= tool_results.len() == tool_calls.len();
            if let Some(outcomes) = terminal_outcomes.as_mut()
                && tool_results.iter().any(|result| command_run_results_empty(&result.result)) {
                // Empty results are omitted from the context log below, not
                // evidence that an accepted command had no uncertain outcome.
                outcomes.hard_veto = true;
            }
            let pending_compact_contexts =
                extract_compact_context_results(&mut tool_results, Some(&runtime));
            let terminal_task_status = tool_results
                .iter()
                .find_map(|result| command_run_result_terminal_task_status(&result.result));
            if let Some(status) = terminal_task_status.as_deref() {
                agent_marked_task_done = status == "done";
            }
            let terminal_status_followed_command = tool_results
                .iter()
                .any(|result| command_run_result_has_command(&result.result));

            for (index, tool_result) in tool_results.iter().enumerate() {
                if command_run_results_empty(&tool_result.result) {
                    continue;
                }
                accumulate_tool_result_with_provider_metadata(
                    session,
                    &tool_result.tool_name,
                    tool_result.arguments.clone(),
                    tool_result.result.clone(),
                    tool_result.success,
                    tool_result.error.clone(),
                    Some(&runtime.runtime_id),
                    tool_calls
                        .get(index)
                        .and_then(|tool_call| tool_call.provider_metadata.clone()),
                )?;
            }
            persist_session_checkpoint(&mut session_delta_writer, session, "tool_results")?;

            if let Some(outcomes) = terminal_outcomes.as_mut() { outcomes.observe_session(session); }
            if terminal_evidence_boundary_proven(
                evidence_only_delivery_proven,
                session,
                &runtime,
                &tool_results,
                terminal_task_status.as_deref(),
                !pending_compact_contexts.is_empty(),
                visible_reply_before_tool.is_none() || visible_reply_published_before_terminal_status,
            ) && let Some(terminal_status) = terminal_outcomes.as_ref()
                .and_then(|outcomes| outcomes.delivery_status(session, &runtime, &tool_results))
            {
                let evidence = TerminalEvidenceRecord {
                    kind: "nokiy.terminal_evidence",
                    schema_version: "nokiy_terminal_evidence_v1",
                    session_id: &session.session_id,
                    runtime_id: &runtime.runtime_id,
                    terminal_status,
                    delivery_mode: "evidence_only",
                    parent_acceptance_required: true,
                    final_summary_turn_executed: false,
                };
                let raw = serde_json::to_string(&evidence)
                    .map_err(|error| format!("failed to encode terminal evidence: {error}"))?;
                // Runtime completion is not business acceptance. Keep blocked
                // outcomes in the marker/history without fabricating a runtime
                // failure or allowing a task-done-gated workspace commit.
                agent_marked_task_done = terminal_status == "done";
                // Persist the terminal runtime state in the same acknowledged
                // checkpoint as the marker, not an intermediate running state.
                session.transition(final_session_state, Utc::now())?;
                session.push_log(raw, Utc::now());
                // The tool-results checkpoint and runtime-feed seal above must
                // succeed before this record, and this checkpoint must succeed
                // before omitting the provider turn. Normal completion/cleanup
                // below is still required; parent acceptance remains pending.
                persist_session_checkpoint(&mut session_delta_writer, session, "terminal_evidence")?;
                terminal_evidence_sealed = true;
                info!(session_id = %session.session_id, runtime_id = %runtime.runtime_id,
                    "durable evidence-only terminal delivery sealed; parent acceptance required");
                break;
            }

            if pending_compact_contexts.is_empty()
                && should_end_turn_without_task_status_backfill(
                    &tool_results,
                    terminal_task_status.as_deref(),
                    visible_reply_before_tool.as_deref(),
                    visible_reply_published_before_terminal_status,
                )
            {
                info!(
                    session_id = %session.session_id,
                    turn = turn,
                    runtime_id = %runtime.runtime_id,
                    "single terminal task_status followed a published non-empty assistant reply; ending turn without tool-result backfill"
                );
                break;
            }

            if !pending_compact_contexts.is_empty() {
                for pending in &pending_compact_contexts {
                    compact_session_context_with_agent_message_and_capabilities(
                        session,
                        &pending.summary,
                        pending.agent_message_content.as_deref().map(|content| {
                            CompactContextAgentMessage {
                                content,
                                timestamp: pending.agent_message_timestamp,
                            }
                        }),
                        &active_agent_capabilities,
                    )?;
                }
                persist_session_checkpoint(&mut session_delta_writer, session, "compact_context")?;
                info!(
                    session_id = %session.session_id,
                    turn = turn,
                    runtime_id = %runtime.runtime_id,
                    "compact_context applied after persisted tool results; continuing task with rebuilt compacted context"
                );

                let context_output = build_context(ContextInput {
                    session,
                    runtime: &runtime,
                    additional_messages: Vec::new(),
                })?;

                current_messages = messages_with_initial_context_prefix(
                    &initial_messages,
                    context_output.messages,
                    &original_user_task,
                );
                continue;
            }

            if let Some(summary) =
                auto_compact_summary_after_new_context(session, &runtime, &tool_results)
            {
                compact_session_context_automatically_with_capabilities(
                    session,
                    &summary,
                    &active_agent_capabilities,
                )?;
                persist_session_checkpoint(
                    &mut session_delta_writer,
                    session,
                    "auto_compact_context",
                )?;
                info!(
                    session_id = %session.session_id,
                    turn = turn,
                    runtime_id = %runtime.runtime_id,
                    "automatic context compaction applied after new tool context exceeded active limit; continuing task"
                );

                let context_output = build_context(ContextInput {
                    session,
                    runtime: &runtime,
                    additional_messages: Vec::new(),
                })?;

                current_messages = messages_with_initial_context_prefix(
                    &initial_messages,
                    context_output.messages,
                    &original_user_task,
                );
                continue;
            }

            let context_output = build_context(ContextInput {
                session,
                runtime: &runtime,
                additional_messages: Vec::new(),
            })?;

            current_messages = messages_with_initial_context_prefix(
                &initial_messages,
                context_output.messages,
                &original_user_task,
            );
            if should_auto_complete_non_planning_doing_after_tool_turn(
                session.goal_mode,
                supports_planning,
                terminal_task_status.as_deref(),
                visible_reply_published_before_terminal_status,
                terminal_status_followed_command,
            ) {
                if complete_active_doing_task_after_non_planning_reply(session, true) {
                    persist_session_checkpoint(
                        &mut session_delta_writer,
                        session,
                        "task_auto_completed",
                    )?;
                }
                info!(
                    session_id = %session.session_id,
                    turn = turn,
                    supports_planning = supports_planning,
                    supports_task_status = supports_task_status,
                    "non-planning agent returned visible final text without a command_run result that needs backfill; active task was auto-completed and loop ended"
                );
                break;
            }
            if matches!(terminal_task_status.as_deref(), Some("done" | "question")) {
                let final_response_published = if terminal_status_needs_final_response_turn(
                    terminal_task_status.as_deref(),
                    visible_reply_published_before_terminal_status,
                    terminal_status_followed_command,
                ) {
                    run_terminal_final_response_turn(
                        agents,
                        session,
                        &current_messages,
                        redis_url,
                        &original_user_task,
                        runtime_event_writer.as_mut(),
                        &mut session_delta_writer,
                    )?
                } else {
                    true
                };
                if !final_response_published {
                    warn!(
                        session_id = %session.session_id,
                        runtime_id = %runtime.runtime_id,
                        status = terminal_task_status.as_deref().unwrap_or("unknown"),
                        "terminal task_status produced no user-facing reply; suppressing internal fallback text"
                    );
                }
                info!(
                    session_id = %session.session_id,
                    turn = turn,
                    status = terminal_task_status.as_deref().unwrap_or("unknown"),
                    "terminal task_status returned and no next executable task exists; ending loop"
                );
                break;
            } else if terminal_task_status.as_deref() == Some("doing") {
                if let Some(next_task) = active_doing_task_user_message(session) {
                    record_task_focus_message_for_terminal_done(session, &next_task, false);
                    persist_session_checkpoint(&mut session_delta_writer, session, "task_focus")?;
                }
            } else if let Some(next_task) = active_task_user_message(session) {
                record_task_focus_message_for_terminal_done(session, &next_task, false);
                persist_session_checkpoint(&mut session_delta_writer, session, "task_focus")?;
            }
        } else {
            let visible_reply_publication = visible_runtime_reply(&runtime).map(|content| {
                let result = publish_agent_message_from_runtime(
                    &session.session_id,
                    &runtime,
                    content,
                    String::new(),
                    feed_publisher.as_ref(),
                );
                if let Err(error) = &result {
                    warn!(
                        session_id = %session.session_id,
                        runtime_id = %runtime.runtime_id,
                        error = %error,
                        "failed to publish final assistant message"
                    );
                }
                result
            });
            evidence_only_delivery_proven &= !matches!(visible_reply_publication.as_ref(), Some(Err(_)));
            seal_runtime_feed(&mut runtime_event_writer, &runtime.runtime_id)?;
            if let Some(outcomes) = terminal_outcomes.as_mut() { outcomes.observe_session(session); }
            let compaction_summary = auto_compact_summary_after_new_context(session, &runtime, &[]);
            if let Some(summary) = &compaction_summary {
                compact_session_context_automatically_with_capabilities(
                    session,
                    summary,
                    &active_agent_capabilities,
                )?;
                persist_session_checkpoint(
                    &mut session_delta_writer,
                    session,
                    "auto_compact_context",
                )?;
                info!(
                    session_id = %session.session_id,
                    turn = turn,
                    runtime_id = %runtime.runtime_id,
                    "automatic context compaction applied after new assistant context exceeded active limit"
                );
            }

            let context_output = build_context(ContextInput {
                session,
                runtime: &runtime,
                additional_messages: Vec::new(),
            })?;

            current_messages = messages_with_initial_context_prefix(
                &initial_messages,
                context_output.messages,
                &original_user_task,
            );
            if should_continue_after_no_tool_compaction(
                compaction_summary.is_some(),
                visible_reply_publication.as_ref(),
            ) {
                continue;
            }

            if planning_child_depth() > 0 {
                info!(
                    session_id = %session.session_id,
                    turn = turn,
                    "planning child turn completed without tool calls, ending child session without synthesized user receipt"
                );
                break;
            }

            let has_visible_reply = visible_runtime_reply(&runtime).is_some();

            let has_active_doing_task = active_doing_task_user_message(session).is_some();
            if has_visible_reply {
                if complete_active_doing_task_after_non_planning_reply(
                    session,
                    !session.goal_mode && !supports_planning,
                ) {
                    persist_session_checkpoint(
                        &mut session_delta_writer,
                        session,
                        "task_auto_completed",
                    )?;
                }
                info!(
                    session_id = %session.session_id,
                    turn = turn,
                    supports_planning = supports_planning,
                    supports_task_status = supports_task_status,
                    has_active_doing_task = has_active_doing_task,
                    "turn completed without command_run but produced a user-visible reply; ending session after any required task_status backfill"
                );
                break;
            }
            if !has_active_doing_task {
                if should_retry_no_tool_task_status(
                    session,
                    supports_planning,
                    supports_task_status,
                    false,
                ) && should_continue_no_tool_task_status_retry(no_tool_retries)
                {
                    no_tool_retries = no_tool_retries.saturating_add(1);
                    push_no_tool_task_status_retry_message(&mut current_messages, session);
                    warn!(
                        session_id = %session.session_id,
                        turn = turn,
                        runtime_id = %runtime.runtime_id,
                        no_tool_retries = no_tool_retries,
                        "goal-mode turn returned no tool calls and no task_status marker; retrying until task_status settles the goal"
                    );
                    continue;
                }
                info!(
                    session_id = %session.session_id,
                    turn = turn,
                    "turn completed without command_run and no task_status doing marker; ending session"
                );
                break;
            }

            if !session.goal_mode && !supports_planning {
                if complete_active_doing_task_after_non_planning_reply(session, false) {
                    persist_session_checkpoint(
                        &mut session_delta_writer,
                        session,
                        "task_auto_completed",
                    )?;
                }
                info!(
                    session_id = %session.session_id,
                    turn = turn,
                    supports_planning = supports_planning,
                    supports_task_status = supports_task_status,
                    "turn completed without command_run while task_status is still active; non-planning agent ended and active task was settled when a visible reply existed"
                );
                break;
            }

            if !should_retry_no_tool_task_status(
                session,
                supports_planning,
                supports_task_status,
                true,
            ) {
                info!(
                    session_id = %session.session_id,
                    turn = turn,
                    goal_mode = session.goal_mode,
                    supports_planning = supports_planning,
                    supports_task_status = supports_task_status,
                    "turn completed without command_run while task_status is still active; retry mode is not enabled or task_status is unavailable"
                );
                break;
            }

            if should_continue_no_tool_task_status_retry(no_tool_retries) {
                no_tool_retries = no_tool_retries.saturating_add(1);
                push_no_tool_task_status_retry_message(&mut current_messages, session);
                if let Some(next_task) = active_doing_task_user_message(session) {
                    record_task_focus_message(session, &next_task);
                    persist_session_checkpoint(&mut session_delta_writer, session, "task_focus")?;
                }
                warn!(
                    session_id = %session.session_id,
                    turn = turn,
                    runtime_id = %runtime.runtime_id,
                    no_tool_retries = no_tool_retries,
                    "non-final turn returned no tool calls; retrying with normal tool set"
                );
                continue;
            }

            info!(
                session_id = %session.session_id,
                turn = turn,
                "turn completed without command_run after retries, ending session"
            );
            break;
        }
    }

    if !terminal_evidence_sealed {
        session.transition(final_session_state, Utc::now())?;
    }
    persist_session_checkpoint(
        &mut session_delta_writer,
        session,
        if final_session_state == SessionState::Failed {
            "failed"
        } else {
            "completed"
        },
    )?;
    commit_terminal_session_checkpoint(session, final_session_state, agent_marked_task_done);

    Ok(ManasResult {
        agents: agents.to_vec(),
        session: session.clone(),
        final_error,
    })
}

fn commit_terminal_session_checkpoint(
    session: &SessionManagement,
    final_session_state: SessionState,
    agent_marked_task_done: bool,
) -> bool {
    if final_session_state != SessionState::Completed || !agent_marked_task_done {
        info!(
            session_id = %session.session_id,
            state = ?final_session_state,
            agent_marked_task_done = agent_marked_task_done,
            "skipping workspace commit because the runtime did not complete with task_status done"
        );
        return false;
    }
    if !env_flag("TURA_RUNTIME_AUTO_GIT_COMMIT") {
        info!(
            session_id = %session.session_id,
            "skipping workspace commit because auto git commit is disabled"
        );
        return false;
    }
    if crate::router_command_run::command_run_sandbox_enabled() {
        info!(
            session_id = %session.session_id,
            "skipping workspace session checkpoint commit because command_run sandbox is enabled"
        );
        return false;
    }

    match crate::workspace_git::commit_session_checkpoint(session, "completed") {
        Ok(Some(commit)) => {
            info!(
                session_id = %session.session_id,
                commit = %commit,
                "committed workspace session checkpoint"
            );
            true
        }
        Ok(None) => {
            warn!(
                session_id = %session.session_id,
                "workspace session checkpoint completed without a resolved commit hash"
            );
            false
        }
        Err(error) => {
            warn!(
                session_id = %session.session_id,
                error = %error,
                "failed to commit workspace session checkpoint"
            );
            false
        }
    }
}

fn manas_max_turns() -> u64 {
    std::env::var("TURA_MANAS_MAX_TURNS")
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(DEFAULT_MANAS_MAX_TURNS)
}

fn increment_turn_with_fresh_timestamp(session: &mut SessionManagement) {
    session.increment_turn(Utc::now());
}

fn should_retry_no_tool_task_status(
    session: &SessionManagement,
    supports_planning: bool,
    supports_task_status: bool,
    has_active_doing_task: bool,
) -> bool {
    if !supports_task_status {
        return false;
    }
    if session.goal_mode {
        return true;
    }
    supports_planning && has_active_doing_task
}

fn should_continue_no_tool_task_status_retry(no_tool_retries: u64) -> bool {
    no_tool_retries < u64::from(no_tool_retry_limit())
}

fn complete_active_doing_task_after_non_planning_reply(
    session: &mut SessionManagement,
    has_visible_reply: bool,
) -> bool {
    if !has_visible_reply {
        return false;
    }
    let mut task_plan = session.task_plan.clone();
    let Some(task) = task_plan
        .detailed_tasks
        .iter_mut()
        .find(|task| task.status == PlanStatus::Doing)
    else {
        return false;
    };
    task.status = PlanStatus::Done;
    session.replace_task_plan(task_plan, Utc::now());
    true
}

fn should_auto_complete_non_planning_doing_after_tool_turn(
    _goal_mode: bool,
    _supports_planning: bool,
    _terminal_task_status: Option<&str>,
    _visible_reply_already_published: bool,
    _terminal_status_followed_command: bool,
) -> bool {
    // A `doing` task_status is only a progress update. It must be replayed to the
    // next model turn so newly activated manuals and task context can be used.
    false
}

fn should_end_turn_without_task_status_backfill(
    tool_results: &[crate::tool_router::execute_tool::ToolExecutionResult],
    terminal_task_status: Option<&str>,
    visible_reply: Option<&str>,
    visible_reply_already_published: bool,
) -> bool {
    if !visible_reply_already_published {
        return false;
    }

    let Some(status @ ("done" | "question")) = terminal_task_status else {
        return false;
    };

    visible_reply.is_some_and(|reply| !reply.trim().is_empty())
        && command_run_has_only_terminal_task_status_result(tool_results, status)
}

fn command_run_has_only_terminal_task_status_result(
    tool_results: &[crate::tool_router::execute_tool::ToolExecutionResult],
    status: &str,
) -> bool {
    let [tool_result] = tool_results else {
        return false;
    };
    tool_result.tool_name == crate::manas::COMMAND_RUN_TOOL
        && command_run_result_is_single_task_status(&tool_result.result, status)
}

fn messages_with_initial_context_prefix(
    initial_messages: &[serde_json::Value],
    session_messages: Vec<serde_json::Value>,
    _original_user_task: &str,
) -> Vec<serde_json::Value> {
    let mut messages = initial_messages
        .iter()
        .filter(|message| {
            let role = message.get("role").and_then(|role| role.as_str());
            role == Some("developer")
        })
        .cloned()
        .collect::<Vec<_>>();
    let overlap = (1..=messages.len().min(session_messages.len()))
        .rev()
        .find(|&length| messages[messages.len() - length..] == session_messages[..length])
        .unwrap_or(0);
    messages.extend(session_messages.into_iter().skip(overlap));
    messages
}

fn append_active_runtime_prompt_manual_context(
    session: &mut SessionManagement,
    current_messages: &mut Vec<serde_json::Value>,
) -> Result<(), String> {
    runtime_prompt_manual::append_missing_runtime_prompt_manuals(session, Some(current_messages))
        .map(|_| ())
}

fn run_terminal_final_response_turn(
    agents: &[AgentManagement],
    session: &mut SessionManagement,
    current_messages: &[serde_json::Value],
    redis_url: &str,
    original_user_task: &str,
    mut runtime_event_writer: Option<&mut RuntimeEventWriter>,
    session_delta_writer: &mut Option<SessionDeltaWriter>,
) -> Result<bool, String> {
    let (runtime, _tool_calls) = execute_turn(
        agents,
        session,
        current_messages,
        original_user_task,
        Some(terminal_final_response::TERMINAL_FINAL_RESPONSE),
        redis_url,
        false,
        true,
        true,
        None,
        None,
        None,
        runtime_event_writer.as_deref_mut(),
    )?;
    let feed_publisher = runtime_event_writer
        .as_deref_mut()
        .map(|writer| {
            writer.feed_publisher(
                &runtime.runtime_id,
                &frontend_session_id(&runtime.session_id),
            )
        })
        .transpose()?;
    let visible_text = visible_runtime_reply(&runtime);
    let has_visible_text = visible_text
        .as_ref()
        .map(|text| !text.trim().is_empty())
        .unwrap_or(false);
    let visible_text = visible_text.filter(|text| !text.trim().is_empty());
    accumulate_session_from_runtime(session, &runtime, true)?;
    session.increment_turn(Utc::now());
    persist_session_checkpoint(session_delta_writer, session, "terminal_final_response")?;
    if let Some(content) = visible_text
        && let Err(error) = publish_agent_message_from_runtime(
            &session.session_id,
            &runtime,
            content,
            String::new(),
            feed_publisher.as_ref(),
        )
    {
        warn!(
            session_id = %session.session_id,
            runtime_id = %runtime.runtime_id,
            error = %error,
            "failed to publish terminal final response assistant message"
        );
    }
    publish_runtime_usage_record(session, &runtime, feed_publisher.as_ref());
    if let Some(writer) = runtime_event_writer {
        writer.seal_runtime(&runtime.runtime_id)?;
    }
    Ok(has_visible_text)
}

fn runtime_feed_publisher(
    runtime_event_writer: &mut Option<RuntimeEventWriter>,
    runtime: &RuntimeAggregate,
) -> Result<Option<RuntimeFeedPublisher>, String> {
    runtime_event_writer
        .as_mut()
        .map(|writer| {
            writer.feed_publisher(
                &runtime.runtime_id,
                &frontend_session_id(&runtime.session_id),
            )
        })
        .transpose()
}

fn seal_runtime_feed(
    runtime_event_writer: &mut Option<RuntimeEventWriter>,
    runtime_id: &str,
) -> Result<(), String> {
    runtime_event_writer
        .as_mut()
        .map_or(Ok(()), |writer| writer.seal_runtime(runtime_id))
}

fn visible_runtime_reply(runtime: &RuntimeAggregate) -> Option<String> {
    user_visible_runtime_text(&runtime.text)
        .or_else(|| {
            runtime
                .output
                .as_ref()
                .and_then(user_visible_runtime_output_text)
        })
        .map(|text| text.trim().to_string())
        .filter(|text| !text.is_empty())
}

fn should_continue_after_no_tool_compaction<T, E>(
    context_compacted: bool,
    visible_reply_publication: Option<&Result<T, E>>,
) -> bool {
    context_compacted && !matches!(visible_reply_publication, Some(Ok(_)))
}

fn auto_compact_summary_after_new_context(
    session: &SessionManagement,
    runtime: &RuntimeAggregate,
    tool_results: &[crate::tool_router::execute_tool::ToolExecutionResult],
) -> Option<String> {
    let limit = session.context_tokens.limit;
    if limit == 0 {
        return None;
    }
    let added_tokens = estimated_new_context_tokens(runtime, tool_results);
    if added_tokens == 0 {
        return None;
    }
    let input_tokens = session.context_tokens.input;
    let projected = input_tokens.saturating_add(added_tokens);
    if projected <= limit {
        return None;
    }
    Some(format!(
        "Automatic context checkpoint: provider input was about {input_tokens} tokens, newly persisted context is estimated at about {added_tokens} tokens by bytes/4, and the projected total {projected} exceeds the active context limit {limit}. Continue the same task from the retained timeline above; preserve completed commands and validation results, and do not rerun work unless the current task requires it."
    ))
}

fn estimated_new_context_tokens(
    runtime: &RuntimeAggregate,
    tool_results: &[crate::tool_router::execute_tool::ToolExecutionResult],
) -> u64 {
    let visible_bytes = visible_runtime_reply(runtime)
        .map(|text| text.len() as u64)
        .unwrap_or(0);
    let tool_bytes = tool_results
        .iter()
        .map(|result| {
            let mut result = result.clone();
            result.result = sanitize_tool_callback_output(&result.result);
            serde_json::to_string(&result)
                .map(|text| text.len() as u64)
                .unwrap_or(0)
        })
        .sum::<u64>();
    estimated_tokens_from_bytes_u64(visible_bytes.saturating_add(tool_bytes))
}

#[derive(serde::Serialize)]
struct TerminalEvidenceRecord<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    schema_version: &'static str,
    session_id: &'a str,
    runtime_id: &'a str,
    terminal_status: &'static str,
    delivery_mode: &'static str,
    parent_acceptance_required: bool,
    final_summary_turn_executed: bool,
}

fn terminal_tool_outcome_proven(
    tool_name: &str,
    success: bool,
    error: Option<&str>,
    arguments: &serde_json::Value,
    output: &serde_json::Value,
) -> bool {
    if !success || error.is_some() || !terminal_payload_proven(output) {
        return false;
    }
    if tool_name != crate::manas::COMMAND_RUN_TOOL {
        // Opaque/delegated tool effects are not proven by a wrapper bool.
        return false;
    }
    let Some(commands) = arguments.get("commands").and_then(serde_json::Value::as_array) else {
        return false;
    };
    let Some(results) = output.get("results").and_then(serde_json::Value::as_array) else {
        return false;
    };
    !commands.is_empty()
        && commands.len() == results.len()
        && commands.iter().zip(results).all(|(command, result)| {
            terminal_command_outcome_proven(command, result)
        })
}

fn terminal_command_outcome_proven(command: &Value, result: &Value) -> bool {
    let kind = command.get("command_type").and_then(Value::as_str);
    kind.is_some()
        && result.get("command_type").and_then(Value::as_str) == kind
        && result.get("output").is_some_and(|output| !output.is_null())
        && result.get("success").and_then(Value::as_bool) == Some(true)
        && terminal_payload_proven(result)
        && (!matches!(kind, Some("shell_command" | "zsh" | "bash" | "powershell"))
            || result.pointer("/output/exit_code").and_then(Value::as_i64) == Some(0))
}

fn terminal_receipt_closed(value: &Value, state: &str) -> bool {
    value.is_object()
        && value["schema_version"] == "tura_command_terminal_receipt_v1"
        && value["outcome"] == "known"
        && value["terminal_state"] == state
        && value["termination_proven"] == true
        && value["process_reaped"] == true
        && value["process_group_empty"] == true
        && value["reconcile_required"] == false
        && value.get("exit_code").is_some()
        && value.get("error").is_none_or(|error| error.is_null() || error.as_str() == Some(""))
        && value.get("unresolved_effects").is_none_or(|effects|
            effects.is_null() || effects.as_array().is_some_and(Vec::is_empty))
        && value.get("timed_out").is_none_or(|value| value.as_bool() == Some(false))
        && value.get("cancelled").is_none_or(|value| value.as_bool() == Some(false))
        && (state != "completed" || value.get("failure_class").is_none_or(|value|
            matches!(value.as_str(), Some("none" | "workload"))))
        && (value["authority_effect"] == "none" || value["authoritative_publication"] == "proven")
}

// Inspect structured outcomes, never JSON-looking stdout or assistant text.
// Missing command success is unknown, not success. A receipt requiring effect
// reconciliation vetoes the shortcut even when the outer tool reports success.
fn terminal_payload_proven(value: &serde_json::Value) -> bool {
    match value {
        Value::Object(object) => object.iter().all(|(key, value)| terminal_payload_field_proven(key, value)),
        Value::Array(values) => values.iter().all(terminal_payload_proven),
        _ => true,
    }
}

fn terminal_payload_field_proven(key: &str, value: &Value) -> bool {
    // Even authenticated non-execution evidence can never prove success.
    if key == "pre_execution_rejection" { return false; }
    if key == "terminal_receipt" {
        // In-process commands may have a null process exit code, not an
        // incomplete outcome or an open effect/cleanup boundary.
        return terminal_receipt_closed(value, "completed")
            && (value["exit_code"].is_null() || value["exit_code"].as_i64() == Some(0));
    }
    let proven = match key {
        "success" => value.as_bool() == Some(true),
        "error" => value.is_null() || value.as_str() == Some(""),
        "exit_code" => value.as_i64() == Some(0),
        "outcome" => value.as_str() == Some("known"),
        "terminal_state" => value.as_str() == Some("completed"),
        "termination_proven" | "process_reaped" | "process_group_empty" => value.as_bool() == Some(true),
        "timed_out" | "reconcile_required" | "cancelled" => value.as_bool() == Some(false),
        "unresolved_effects" => value.is_null() || value.as_array().is_some_and(Vec::is_empty),
        "status" => !matches!(value.as_str(),
            Some("failed" | "error" | "unknown" | "pending" | "running" | "timed_out"
                | "cancelled" | "aborted" | "blocked" | "unresolved")),
        _ => true,
    };
    proven && terminal_payload_proven(value)
}

const MAX_TERMINAL_TRACKED_COMMANDS: usize = 512;
const MAX_TERMINAL_VERIFIERS: usize = 64;
const MAX_TERMINAL_SOURCE_TARGETS: usize = TerminalVerifierProof::MAX_SOURCE_TARGETS;
const MAX_TERMINAL_EVIDENCE_BYTES: usize = 256 * 1024;

fn terminal_identity(value: &Value) -> Option<&str> {
    value.as_str().filter(|text| !text.trim().is_empty() && text.len() <= 256)
}

fn terminal_sha256(text: &str) -> bool {
    text.len() == 64 && text.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn terminal_source_target(text: &str) -> bool {
    !text.is_empty() && text.len() <= 256
        && !text.chars().any(|ch| matches!(ch, '\\' | '\0' | '*' | '?' | '[' | ']'))
        && text.split('/').all(|part| !matches!(part, "" | "." | ".."))
}

// Canonical compact UTF-8 JSON, including EVERY grant field. In particular,
// this is not the J-Space hash function that omits authorization metadata.
struct TerminalCanonicalJson<'a>(&'a Value);

impl serde::Serialize for TerminalCanonicalJson<'_> {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self.0 {
            Value::Object(object) => {
                if object.len() > MAX_TERMINAL_TRACKED_COMMANDS {
                    return Err(serde::ser::Error::custom("terminal evidence object exceeds tracking bound"));
                }
                let sorted = object.iter().map(|(key, value)| (key, TerminalCanonicalJson(value)))
                    .collect::<BTreeMap<_, _>>();
                serde::Serialize::serialize(&sorted, serializer)
            }
            Value::Array(values) => serializer.collect_seq(values.iter().map(TerminalCanonicalJson)),
            value => serde::Serialize::serialize(value, serializer),
        }
    }
}

fn terminal_fingerprint(value: &Value) -> Option<String> {
    struct HashWriter { hash: Sha256, bytes: usize }
    impl std::io::Write for HashWriter {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            if bytes.len() > MAX_TERMINAL_EVIDENCE_BYTES.saturating_sub(self.bytes) {
                return Err(std::io::ErrorKind::InvalidData.into());
            }
            self.bytes += bytes.len();
            self.hash.update(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> { Ok(()) }
    }
    let mut writer = HashWriter { hash: Sha256::new(), bytes: 0 };
    serde_json::to_writer(&mut writer, &TerminalCanonicalJson(value)).ok()?;
    Some(format!("{:x}", writer.hash.finalize()))
}

struct TerminalVerifierBinding {
    authorization: String,
    grants: Vec<String>,
    targets: BTreeSet<String>,
}

impl TerminalVerifierBinding {
    fn from_contract(contract: &Value) -> Option<Self> {
        // Admission already owns validation/authority. Snapshot ONLY the
        // immutable session contract, never a tool's advisory postimage context.
        let authorization = contract["authorization_semantic_sha256"].as_str()?;
        let grants = contract["verifier_commands"].as_array()?;
        let declared = contract["declared_targets"].as_array()?;
        let writes = contract["write_scopes"].as_array()?;
        if !terminal_sha256(authorization) || grants.len() > MAX_TERMINAL_VERIFIERS || !grants.iter().all(Value::is_object)
            || declared.len() > MAX_TERMINAL_SOURCE_TARGETS || writes.len() > MAX_TERMINAL_SOURCE_TARGETS {
            return None;
        }
        let targets = declared.iter().map(|value| {
            value.as_str().filter(|path| terminal_source_target(path)).map(str::to_owned)
        }).collect::<Option<BTreeSet<_>>>()?;
        if targets.len() != declared.len() || !writes.iter().all(|value|
            value.as_str().is_some_and(|path| targets.contains(path))) {
            return None;
        }
        Some(Self {
            authorization: authorization.to_owned(),
            grants: grants.iter().map(terminal_fingerprint).collect::<Option<Vec<_>>>()?,
            targets,
        })
    }
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct TerminalVerifierSelection { verifier_index: usize }

impl TerminalVerifierProof {
    fn source_fingerprint(value: &Value, binding: &TerminalVerifierBinding, index: usize, call_id: &str) -> Option<String> {
        if value["source_postimages"].as_object()?.len() != binding.targets.len() || terminal_fingerprint(value).is_none() {
            return None;
        }
        let proof = Self::parse_bounded(value)?;
        if proof.schema_version != "nokiy_focused_verifier_evidence_v1"
            || proof.authorization_semantic_sha256 != binding.authorization
            || proof.verifier_index != index || binding.grants.get(index)? != &proof.verifier_sha256
            || proof.call_id != call_id
            || !proof.source_postimages.keys().eq(binding.targets.iter())
            || !proof.source_postimages.values().all(|image|
                terminal_sha256(&image.sha256) && image.mode <= 0o177777 && image.bytes <= i64::MAX as u64) {
            return None;
        }
        terminal_fingerprint(&value["source_postimages"])
    }
}

struct TerminalStreamBinding {
    kind: String,
    step: u64,
    provider_call_id: String,
    completed: Option<(usize, String)>,
}

// Evidence projection only: no log rewriting, receipt replay, counter changes,
// persistence, or parent acceptance. Its bounded state survives local compaction;
// a fresh tracker cannot reconstruct omitted history and therefore vetoes it.
struct TerminalOutcomeTracker {
    contract: Option<Value>,
    session_id: String,
    session_directory: std::path::PathBuf,
    binding: Option<TerminalVerifierBinding>,
    observed_entries: u64,
    hard_veto: bool,
    mutation_generation: u64,
    failures: BTreeMap<usize, Option<u64>>,
    postimages: Option<(u64, String)>,
    runtimes: BTreeSet<(String, Option<u64>)>,
    receipts: BTreeSet<String>,
    streamed: BTreeMap<(String, usize), TerminalStreamBinding>,
    streamed_calls: BTreeSet<(String, String)>,
}

impl TerminalOutcomeTracker {
    fn new(session: &SessionManagement) -> Self {
        let mut tracker = Self::unobserved(session);
        tracker.observe_session(session);
        tracker
    }

    fn unobserved(session: &SessionManagement) -> Self {
        let bounded_contract = session.jspace_contract.as_ref().is_none_or(|value| terminal_fingerprint(value).is_some());
        Self {
            contract: bounded_contract.then(|| session.jspace_contract.clone()).flatten(),
            session_id: session.session_id.clone(),
            session_directory: session.session_directory.clone(),
            binding: session.jspace_contract.as_ref().filter(|_| bounded_contract)
                .and_then(TerminalVerifierBinding::from_contract),
            observed_entries: 0,
            hard_veto: !bounded_contract,
            mutation_generation: 0,
            failures: BTreeMap::new(), postimages: None,
            runtimes: BTreeSet::new(), receipts: BTreeSet::new(), streamed: BTreeMap::new(),
            streamed_calls: BTreeSet::new(),
        }
    }

    fn proven(&self) -> bool {
        !self.hard_veto && self.streamed.is_empty()
            && self.failures.values().all(|generation| *generation == Some(self.mutation_generation))
    }

    fn delivery_status(
        &self,
        session: &SessionManagement,
        runtime: &RuntimeAggregate,
        tool_results: &[crate::tool_router::execute_tool::ToolExecutionResult],
    ) -> Option<&'static str> {
        if session.jspace_contract != self.contract || session.session_id != self.session_id
            || session.session_directory != self.session_directory {
            return None;
        }
        if self.proven() { return Some("done"); }
        // Missing/foreign history cannot authenticate even a blocked handoff.
        // Never reset the original tracker or rewrite its failures. A separate
        // projection proves ONLY this current terminal batch, not task success,
        // settled historical effects, parent acceptance, or retry permission.
        if session.session_log_retention.omitted_entries != 0 {
            return None;
        }
        let mut current = Self::unobserved(session);
        let mut batches = 0;
        for entry in &session.session_log {
            let value = entry.value();
            let kind = value["type"].as_str();
            if !value.is_object() || value["schema_version"] == "nokiy_terminal_evidence_v1"
                || kind == Some("nokiy.terminal_evidence")
                || value.get("session_id").is_some_and(|id| id.as_str() != Some(session.session_id.as_str()))
                || (matches!(kind, Some("tool_result" | "streamed_command_event"))
                    && terminal_identity(&value["runtime_id"]).is_none()) {
                return None;
            }
            if value["runtime_id"].as_str() == Some(runtime.runtime_id.as_str()) {
                match kind {
                    Some("tool_result") => { current.observe_batch(value); batches += 1; }
                    Some("streamed_command_event") => current.observe_streamed(value),
                    _ => {}
                }
            }
        }
        (batches != 0 && batches == tool_results.len() && current.proven()).then_some("blocked")
    }

    fn observe_session(&mut self, session: &SessionManagement) {
        if self.hard_veto { return; }
        let omitted = session.session_log_retention.omitted_entries;
        let Some(end) = omitted.checked_add(session.session_log.len() as u64) else {
            self.hard_veto = true; return;
        };
        if session.jspace_contract != self.contract || omitted > self.observed_entries || end < self.observed_entries
            || session.session_id != self.session_id || session.session_directory != self.session_directory {
            self.hard_veto = true; return;
        }
        let start = (self.observed_entries - omitted) as usize;
        for entry in session.session_log.iter().skip(start) {
            let value = entry.value();
            if !value.is_object() || value["schema_version"] == "nokiy_terminal_evidence_v1"
                || value["type"] == "nokiy.terminal_evidence"
                || value.get("session_id").is_some_and(|id| id.as_str() != Some(session.session_id.as_str())) {
                self.hard_veto = true;
            } else {
                match value["type"].as_str() {
                    Some("tool_result") => self.observe_batch(value),
                    Some("streamed_command_event") => self.observe_streamed(value),
                    _ => {}
                }
            }
            if self.hard_veto { break; }
        }
        self.observed_entries = end;
    }

    fn observe_streamed(&mut self, event: &Value) {
        let Some(runtime) = terminal_identity(&event["runtime_id"]) else { self.hard_veto = true; return; };
        let Some(kind) = terminal_identity(&event["command_type"]) else { self.hard_veto = true; return; };
        let Some(step) = event["step"].as_u64().filter(|step| *step > 0) else { self.hard_veto = true; return; };
        let ready = event["status"] == "ready";
        let Some((index, result_index)) = terminal_stream_indices(event) else { self.hard_veto = true; return; };
        if !terminal_stream_header_proven(event) {
            self.hard_veto = true; return;
        }
        let key = (runtime.to_owned(), index);
        if ready {
            let Some(provider_call_id) = terminal_identity(&event["provider_tool_call_id"]) else { self.hard_veto = true; return; };
            if self.streamed.len() >= MAX_TERMINAL_TRACKED_COMMANDS || self.streamed.contains_key(&key)
                || self.streamed_calls.iter().any(|(id, call)| id == runtime && call == provider_call_id)
                || !terminal_stream_identity_matches(event, runtime, provider_call_id)
                || !terminal_payload_proven(event) {
                self.hard_veto = true; return;
            }
            self.streamed.insert(key, TerminalStreamBinding {
                kind: kind.to_owned(), step, provider_call_id: provider_call_id.to_owned(), completed: None,
            });
        } else {
            let Some(result_index) = result_index else { self.hard_veto = true; return; };
            if self.streamed.iter().any(|((id, _), binding)| id == runtime
                && binding.completed.as_ref().is_some_and(|(index, _)| *index == result_index)) {
                self.hard_veto = true; return;
            }
            let result = &event["result"];
            let Some(fingerprint) = terminal_fingerprint(result) else { self.hard_veto = true; return; };
            let Some(binding) = self.streamed.get_mut(&key) else { self.hard_veto = true; return; };
            // Native completed records omit wrapper error. Any supplied copy
            // must agree; the canonical result and receipt remain authoritative.
            if event["status"] != "completed" || binding.completed.is_some() || binding.kind != kind || binding.step != step
                || event["success"].as_bool().is_none() || event["success"] != result["success"]
                || result["command_type"] != kind || result["step"] != step
                || event.get("error").is_some_and(|error| error != &result["error"])
                || !terminal_stream_identity_matches(event, runtime, &binding.provider_call_id) {
                self.hard_veto = true; return;
            }
            binding.completed = Some((result_index, fingerprint));
        }
    }

    fn observe_batch(&mut self, value: &Value) {
        let Some(runtime) = terminal_identity(&value["runtime_id"]) else { self.hard_veto = true; return; };
        let Some(success) = value["success"].as_bool() else { self.hard_veto = true; return; };
        let arguments = &value["input"];
        let output = &value["output"];
        let (Some(commands), Some(results), Some(object)) = (
            arguments["commands"].as_array(), output["results"].as_array(), output.as_object(),
        ) else { self.hard_veto = true; return; };
        let error = value.get("error").filter(|error| !error.is_null());
        if value["tool_name"] != crate::manas::COMMAND_RUN_TOOL || commands.is_empty()
            || commands.len() != results.len() || commands.len() > MAX_TERMINAL_TRACKED_COMMANDS
            || value.get("sequence").is_some_and(|sequence| sequence.as_u64().is_none())
            || self.runtimes.len() >= MAX_TERMINAL_TRACKED_COMMANDS
            || !self.runtimes.insert((runtime.to_owned(), value.get("sequence").and_then(Value::as_u64)))
            || results.iter().any(|result| result["success"].as_bool().is_none())
            || success != results.iter().all(|result| result["success"] == true)
            || error.is_some_and(|error| error.as_str().is_none() || success
                || !results.iter().any(|result| result["success"] == false && result.get("error") == Some(error)))
            || !value.as_object().is_some_and(|object| object.iter().all(|(key, value)|
                // Native context projections repeat historical failures. They
                // are advisory, never additional outcomes or resolution proof.
                matches!(key.as_str(), "success" | "error" | "input" | "output" | "context_cache" | "context_messages")
                    || terminal_payload_field_proven(key, value)))
            || !object.iter().all(|(key, value)| matches!(key.as_str(), "results" | "commands" | "command_events")
                || terminal_payload_field_proven(key, value))
            || output.get("commands").is_some_and(|commands| {
                if value.get("sequence").is_some() {
                    crate::context::strip_tool_reporting_fields(commands.clone()) != arguments["commands"]
                } else {
                    commands != &arguments["commands"]
                }
            })
            || (success && !terminal_tool_outcome_proven(crate::manas::COMMAND_RUN_TOOL, true, None, arguments, output)) {
            self.hard_veto = true; return;
        }
        let stream_count = self.streamed.keys().filter(|(id, _)| id == runtime).count();
        if (stream_count != 0 && stream_count != commands.len())
            || output.get("command_events").is_some_and(|events|
                !self.streamed_batch_matches(runtime, commands.len(), events)) {
            self.hard_veto = true; return;
        }
        let mut execution_order = Vec::with_capacity(commands.len());
        let mut mutation_steps = BTreeSet::new();
        let mut verifier_steps = BTreeSet::new();
        for (index, (command, result)) in commands.iter().zip(results).enumerate() {
            let Some(kind) = terminal_identity(&command["command_type"]) else { self.hard_veto = true; return; };
            let Some(step) = terminal_command_step(command) else { self.hard_veto = true; return; };
            if result["command_type"] != kind || terminal_command_step(result) != Some(step) {
                self.hard_veto = true; return;
            }
            if stream_count != 0 {
                let Some(binding) = self.streamed.remove(&(runtime.to_owned(), index)) else { self.hard_veto = true; return; };
                if binding.kind != kind || binding.step != step
                    || binding.completed.as_ref().is_none_or(|(result_index, fingerprint)|
                        *result_index >= commands.len() || terminal_fingerprint(result).as_ref() != Some(fingerprint))
                    || value.get("provider_tool_call_id").is_some_and(|id| id.as_str() != Some(binding.provider_call_id.as_str())) {
                    self.hard_veto = true; return;
                }
                let call = (runtime.to_owned(), binding.provider_call_id);
                if self.streamed_calls.len() >= MAX_TERMINAL_TRACKED_COMMANDS && !self.streamed_calls.contains(&call) {
                    self.hard_veto = true; return;
                }
                self.streamed_calls.insert(call);
            }
            if kind == "focused_verifier" { verifier_steps.insert(step); }
            if terminal_command_may_mutate(kind, &result["output"]) { mutation_steps.insert(step); }
            execution_order.push((step, index));
        }
        // Commands within a step can overlap. Array order is NOT a happens-before proof.
        if !mutation_steps.is_disjoint(&verifier_steps) { self.hard_veto = true; return; }
        // Fold authenticated outcomes by scheduled step, not presentation order.
        // Keep original command/result indices and streamed bindings unchanged.
        execution_order.sort_unstable();
        let mut terminal_status_guard = code_tools::command_run::CommandRunTerminalStatusGuard::default();
        for (_, index) in execution_order {
            let (command, result) = (&commands[index], &results[index]);
            let kind = command["command_type"].as_str().unwrap_or_default();
            if result.get("output").is_none_or(Value::is_null) {
                // Native guards reject before dispatch and therefore have no
                // output/receipt. Replay ONLY this batch's already-settled
                // outcomes; neither the error text nor historical failures
                // authenticate a non-attempt by themselves.
                let reported_command = output.get("commands").and_then(|commands| commands.get(index)).unwrap_or(command);
                if success || value["sequence"].as_u64().is_none() || kind != "task_status"
                    || !Self::done_guard_rejection_proven(&terminal_status_guard, commands, index, reported_command, result) {
                    self.hard_veto = true; return;
                }
            } else if kind == "focused_verifier" {
                self.observe_verifier(command, result);
            } else if !success && !terminal_command_outcome_proven(command, result) {
                // Authenticate ONLY a singleton router-owned syntax/shape
                // non-attempt. This never proves a read succeeded or resolves a
                // verifier failure, and cannot forgive a mixed mutating batch.
                if commands.len() != 1 || kind != "source_read"
                    || !self.observe_source_read_rejection(runtime, arguments, command, result) {
                    self.hard_veto = true;
                }
            }
            self.observe_receipts(&result["output"]);
            if terminal_command_may_mutate(kind, &result["output"]) {
                match self.mutation_generation.checked_add(1) {
                    Some(generation) => self.mutation_generation = generation,
                    None => self.hard_veto = true,
                }
            }
            if self.hard_veto { return; }
            // Failed verifiers enter here only after receipt/cleanup/grant
            // validation. Their unresolved state remains in self.failures.
            terminal_status_guard.observe_step(command.get("step").and_then(Value::as_u64));
            terminal_status_guard.observe_result(result.get("success").and_then(Value::as_bool));
        }
    }

    fn done_guard_rejection_proven(
        guard: &code_tools::command_run::CommandRunTerminalStatusGuard,
        commands: &[Value],
        index: usize,
        reported_command: &Value,
        result: &Value,
    ) -> bool {
        let Some(command) = commands.get(index) else { return false; };
        let (step, done) = code_tools::command_run::CommandRunTerminalStatusGuard::command_metadata(command);
        let Some(step) = step.filter(|_| done && index + 1 == commands.len()) else { return false; };
        if !commands[..index].iter().all(|command| command["step"].as_u64()
            .is_some_and(|prior| prior > 0 && prior < step))
            || result["success"] != false || result.get("output").is_some_and(|output| !output.is_null()) {
            return false;
        }
        // Explicit sole-final ordering excludes ORDER_ERROR. An error here
        // can only come from a validated failure earlier in this same batch.
        let Some(error) = guard.done_error(Some(step), true) else { return false; };
        result["error"].as_str() == Some(error)
            && ["id", "command_id", "command_run_id", "provider_tool_call_id", "command_index"].iter()
                .all(|key| result.get(*key) == reported_command.get(*key))
            && result.as_object().is_some_and(|object| object.iter().all(|(key, value)| match key.as_str() {
                "command_type" => value == "task_status",
                "step" => value.as_u64() == Some(step),
                "success" => value.as_bool() == Some(false),
                "error" => value.as_str() == Some(error),
                "output" => value.is_null(),
                "command" => value == reported_command,
                "command_index" => value.as_u64() == Some(index as u64),
                "id" | "command_id" | "command_run_id" | "provider_tool_call_id" => terminal_identity(value).is_some(),
                // Non-attempts have no receipt, payload, or additional effects.
                _ => false,
            }))
    }

    fn observe_source_read_rejection(&mut self, runtime: &str, arguments: &Value, command: &Value, result: &Value) -> bool {
        use code_tools::commands::source_read::{SourceReadPreExecutionRejection, source_read_recovery_authorization};
        let Some(authorization) = self.contract.as_ref().and_then(source_read_recovery_authorization)
            else { return false; };
        let Some(proof) = SourceReadPreExecutionRejection::parse_bounded(&result["output"]["pre_execution_rejection"])
            else { return false; };
        if arguments.get("execution_id").is_some_and(|id| id.as_str() != Some(proof.execution_id())) {
            return false;
        }
        let Ok(store) = tura_path::command_receipts::ReceiptStore::open_existing(&self.session_directory)
            else { return false; };
        proof.authenticate(&store, &self.session_id, runtime, authorization, command, result)
            && self.receipts.len() < MAX_TERMINAL_TRACKED_COMMANDS
            && self.receipts.insert(proof.call_id().to_owned())
    }

    fn observe_verifier(&mut self, command: &Value, result: &Value) {
        let output = &result["output"];
        let passed = result["success"] == true;
        if passed && output.get("verification_evidence").is_none() {
            // The wire proof is optional. Preserve legacy/native successes under
            // the original validator, but NEVER use them to resolve a failure.
            if !terminal_command_outcome_proven(command, result) { self.hard_veto = true; }
            return;
        }
        let Some(line) = command["command_line"].as_str().filter(|line| line.len() <= 256) else { self.hard_veto = true; return; };
        let Ok(selection) = serde_json::from_str::<TerminalVerifierSelection>(line) else { self.hard_veto = true; return; };
        let Some(binding) = self.binding.as_ref() else { self.hard_veto = true; return; };
        let index = selection.verifier_index;
        let receipt = &output["terminal_receipt"];
        let Some(call_id) = terminal_identity(&receipt["call_id"]) else { self.hard_veto = true; return; };
        let Some(exit_code) = receipt["exit_code"].as_i64() else { self.hard_veto = true; return; };
        let state = if passed { "completed" } else { "failed" };
        if binding.grants.get(index).is_none() || command["step"].as_u64().is_none_or(|step| step == 0)
            || ["workdir", "cwd", "timeout_ms", "stall_timeout_ms", "arguments"].iter()
                .any(|key| command.get(*key).is_some_and(|value| !value.is_null()))
            || output["executor"] != "parent_focused_verifier" || !terminal_receipt_closed(receipt, state)
            || receipt["termination_origin"] != "parent_verifier"
            || (!passed && receipt["failure_class"] != "workload")
            || (passed && !matches!(receipt["failure_class"].as_str(), Some("none" | "workload")))
            || !matches!(receipt["authoritative_publication"].as_str(), Some("proven" | "unproven"))
            || receipt["staging_authority"] != "none"
            || receipt["authority_effect"] != "none" || !(0..=i32::MAX as i64).contains(&exit_code)
            || passed != (exit_code == 0)
            || result.get("error").is_some_and(|error| !error.is_null() && (error.as_str().is_none() || (passed && error != "")))
            || !result.as_object().is_some_and(|object| object.iter().all(|(key, value)|
                matches!(key.as_str(), "success" | "error" | "output") || terminal_payload_field_proven(key, value)))
            || !output.as_object().is_some_and(|object| object.iter().all(|(key, value)| match key.as_str() {
                "terminal_receipt" => true,
                "exit_code" => value.as_i64() == Some(exit_code),
                "success" => value.as_bool() == Some(passed),
                "terminal_state" => value.as_str() == Some(state),
                _ => terminal_payload_field_proven(key, value),
            })) {
            self.hard_veto = true; return;
        }
        // The parent route produces failed/known/positive-exit receipts only for
        // settled workload failures. Lost channels, signals, and cleanup failures
        // have unknown/terminated/negative-exit outcomes and cannot enter here.
        if !passed {
            if output.get("verification_evidence").is_some() { self.hard_veto = true; return; }
            self.failures.insert(index, None);
        } else if let Some(proof) = output.get("verification_evidence") {
            let Some(postimages) = TerminalVerifierProof::source_fingerprint(proof, binding, index, call_id) else {
                self.hard_veto = true; return;
            };
            if self.postimages.as_ref().is_some_and(|(generation, previous)|
                *generation == self.mutation_generation && previous != &postimages) {
                self.hard_veto = true; return;
            }
            self.postimages = Some((self.mutation_generation, postimages));
            if let Some(generation) = self.failures.get_mut(&index) { *generation = Some(self.mutation_generation); }
        }
    }

    fn streamed_batch_matches(&self, runtime: &str, commands: usize, value: &Value) -> bool {
        let Some(events) = value.as_array() else {
            // Native recording omits duplicate event payloads, not their audit
            // records. Require the exact marker AND every retained completion;
            // observe_batch still binds each fingerprint to the canonical result.
            let Some(marker) = value.as_object() else { return false; };
            if marker.len() != 3 || value["omitted_from_record"] != true
                || value["reason"] != COMMAND_EVENTS_OMISSION_REASON {
                return false;
            }
            let streamed = self.streamed.keys().filter(|(id, _)| id == runtime).count();
            return if value["count"].as_u64() == Some(0) {
                streamed == 0
            } else {
                value["count"].as_u64() == Some(commands as u64 * 2) && streamed == commands
                    && (0..commands).all(|index| self.streamed.get(&(runtime.to_owned(), index))
                        .and_then(|binding| binding.completed.as_ref())
                        .is_some_and(|(result_index, _)| *result_index < commands))
            };
        };
        if events.is_empty() { return !self.streamed.keys().any(|(id, _)| id == runtime); }
        if events.len() != commands * 2 { return false; }
        let mut seen = BTreeSet::new();
        events.iter().all(|event| {
            let Some((index, result_index)) = terminal_stream_indices(event) else { return false; };
            let ready = event["status"] == "ready";
            let Some(binding) = self.streamed.get(&(runtime.to_owned(), index)) else { return false; };
            seen.insert((index, ready)) && terminal_stream_header_proven(event) && event["command_type"] == binding.kind
                && event["step"].as_u64() == Some(binding.step)
                && terminal_stream_identity_matches(event, runtime, &binding.provider_call_id)
                && if ready {
                    terminal_identity(&event["provider_tool_call_id"]) == Some(binding.provider_call_id.as_str())
                        && terminal_payload_proven(event)
                } else {
                    binding.completed.as_ref().is_some_and(|(completed_index, fingerprint)|
                        result_index == Some(*completed_index) && *completed_index < commands
                            && terminal_fingerprint(&event["result"]).as_ref() == Some(fingerprint))
                        && event["success"] == event["result"]["success"]
                        && event.get("error").is_none_or(|error| error == &event["result"]["error"])
                }
        })
    }

    fn observe_receipts(&mut self, value: &Value) {
        match value {
            Value::Object(object) => for (key, value) in object {
                if key == "terminal_receipt" {
                    let Some(id) = terminal_identity(&value["call_id"]) else { self.hard_veto = true; return; };
                    if self.receipts.len() >= MAX_TERMINAL_TRACKED_COMMANDS || !self.receipts.insert(id.to_owned()) {
                        self.hard_veto = true; return;
                    }
                } else { self.observe_receipts(value); }
                if self.hard_veto { return; }
            },
            Value::Array(values) => for value in values { self.observe_receipts(value); if self.hard_veto { return; } },
            _ => {}
        }
    }
}

fn terminal_stream_header_proven(event: &Value) -> bool {
    event.as_object().is_some_and(|object| object.iter().all(|(key, value)|
        matches!(key.as_str(), "result" | "success" | "error" | "status") || terminal_payload_field_proven(key, value)))
}

fn terminal_bounded_index(value: &Value) -> Option<usize> {
    let index = value.as_u64().and_then(|index| usize::try_from(index).ok())?;
    (index < MAX_TERMINAL_TRACKED_COMMANDS).then_some(index)
}

fn terminal_stream_indices(event: &Value) -> Option<(usize, Option<usize>)> {
    let ready = match event["status"].as_str()? {
        "ready" => true,
        "completed" => false,
        _ => return None,
    };
    let result_index = match event.get("result_index") {
        Some(value) => Some(terminal_bounded_index(value)?),
        None if ready => None,
        None => return None,
    };
    let identities = [event, &event["command"], &event["result"], &event["result"]["command"]];
    // Native completions distinguish stable command position from arrival
    // position. Legacy result-index-only records retain their positional form;
    // any supplied stable identity copy must agree, never silently fall back.
    let command_index = if ready {
        event.get("command_index")?
    } else {
        identities.iter().find_map(|value| value.get("command_index"))
            .or_else(|| event.get("result_index"))?
    };
    let index = terminal_bounded_index(command_index)?;
    (identities.iter().all(|value| value.get("command_index")
        .is_none_or(|value| value.as_u64() == Some(index as u64)))
        && (!ready || result_index.is_none_or(|result_index| result_index == index)))
        .then_some((index, result_index))
}

fn terminal_stream_identity_matches(event: &Value, runtime: &str, provider_call: &str) -> bool {
    [event, &event["command"], &event["result"], &event["result"]["command"]].iter().all(|value|
        value.get("runtime_id").is_none_or(|id| id.as_str() == Some(runtime))
            && value.get("provider_tool_call_id").is_none_or(|id| id.as_str() == Some(provider_call)))
}

fn terminal_command_step(command: &Value) -> Option<u64> {
    match command.get("step") {
        None => Some(1),
        Some(step) => step.as_u64().filter(|step| *step > 0),
    }
}

fn terminal_command_may_mutate(kind: &str, output: &Value) -> bool {
    !matches!(kind, "source_read" | "focused_verifier" | "task_status" | "planning")
        || output.get("changes").is_some_and(|changes| !changes.as_array().is_some_and(Vec::is_empty))
}

fn terminal_results_end_in_done(
    tool_results: &[crate::tool_router::execute_tool::ToolExecutionResult],
) -> bool {
    tool_results.last()
        .filter(|result| result.tool_name == crate::manas::COMMAND_RUN_TOOL)
        .and_then(|result| result.result.get("results").and_then(serde_json::Value::as_array))
        .and_then(|results| results.last())
        .filter(|result| result["command_type"] == "task_status")
        .and_then(|result| result.pointer("/output/task_status/status")
            .and_then(serde_json::Value::as_str)) == Some("done")
}

fn terminal_evidence_boundary_proven(
    delivery_proven: bool,
    session: &SessionManagement,
    runtime: &RuntimeAggregate,
    tool_results: &[crate::tool_router::execute_tool::ToolExecutionResult],
    terminal_task_status: Option<&str>,
    pending_compaction: bool,
    visible_reply_delivered: bool,
) -> bool {
    // Feed sealing and the tool-results checkpoint must already have succeeded
    // at the caller. Their errors propagate before reaching this boundary.
    delivery_proven
        && terminal_task_status == Some("done")
        && terminal_results_end_in_done(tool_results)
        && runtime.session_id == session.session_id
        && !runtime.runtime_id.trim().is_empty()
        && !pending_compaction
        && auto_compact_summary_after_new_context(session, runtime, tool_results).is_none()
        && visible_reply_delivered
}

fn terminal_status_needs_final_response_turn(
    terminal_task_status: Option<&str>,
    visible_reply_already_published: bool,
    terminal_status_followed_command: bool,
) -> bool {
    match terminal_task_status {
        Some("done") => true,
        Some("question") => !visible_reply_already_published || terminal_status_followed_command,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::{
        DEFAULT_MANAS_MAX_TURNS, ProviderRetryProgress, auto_compact_summary_after_new_context,
        commit_terminal_session_checkpoint, complete_active_doing_task_after_non_planning_reply,
        increment_turn_with_fresh_timestamp, manas_max_turns, messages_with_initial_context_prefix,
        should_auto_complete_non_planning_doing_after_tool_turn,
        should_continue_after_no_tool_compaction, should_continue_no_tool_task_status_retry,
        should_end_turn_without_task_status_backfill, should_retry_no_tool_task_status,
        terminal_status_needs_final_response_turn, visible_runtime_reply,
        write_provider_retry_progress,
    };
    use crate::manas::tool_catalog::provider_command_run_commands_for_jspace;
    use crate::tool_router::execute_tool::ToolExecutionResult;
    use crate::turn_loop::no_tool_policy::no_tool_retry_limit;
    use chrono::{Duration, Utc};
    use lifecycle::{PlanStatus, SessionInput, SessionManagement, SessionState, TaskStep};
    use lifecycle::{ProviderConfig, ToolChoice};
    use lifecycle::{RuntimeAggregate, RuntimeProviderConfig, UsageReport};
    use serde_json::json;
    use std::collections::BTreeSet;
    use std::path::PathBuf;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn outcome_session() -> SessionManagement {
        let mut session = test_session("session-outcomes");
        // This fixture represents an already-admitted immutable contract. The
        // tracker consumes its binding, and does not create execution authority.
        session.jspace_contract = Some(json!({
            "schema_version": "jspace_contract_v2",
            "authorization_semantic_sha256": "a".repeat(64),
            "verifier_commands": [
                {"argv": ["/usr/bin/python3", "check.py"], "timeout_seconds": 30, "read_scopes": ["src/example.rs"]},
                {"argv": ["/usr/bin/python3", "other.py"], "timeout_seconds": 30, "read_scopes": ["src/example.rs"]}
            ],
            "declared_targets": ["src/example.rs"], "write_scopes": ["src/example.rs"]
        }));
        session
    }

    fn outcome_verifier(session: &SessionManagement, call: &str, index: usize, exit: i32, proof: bool) -> (serde_json::Value, serde_json::Value) {
        let passed = exit == 0;
        let mut output = json!({
            "executor": "parent_focused_verifier", "exit_code": exit,
            "terminal_receipt": {
                "schema_version": "tura_command_terminal_receipt_v1", "call_id": call,
                "outcome": "known", "terminal_state": if passed { "completed" } else { "failed" },
                "failure_class": if passed { "none" } else { "workload" }, "termination_origin": "parent_verifier",
                "termination_proven": true, "process_reaped": true, "process_group_empty": true,
                "reconcile_required": false, "authority_effect": "none", "staging_authority": "none",
                "authoritative_publication": "unproven", "exit_code": exit
            }
        });
        if proof {
            let contract = session.jspace_contract.as_ref().expect("admitted contract");
            let sources = contract["declared_targets"].as_array().expect("targets").iter().map(|path| (
                path.as_str().expect("path").to_owned(), json!({"sha256": "b".repeat(64), "bytes": 28, "mode": 420})
            )).collect::<serde_json::Map<_, _>>();
            output["verification_evidence"] = json!({
                "schema_version": "nokiy_focused_verifier_evidence_v1",
                "authorization_semantic_sha256": contract["authorization_semantic_sha256"],
                "verifier_index": index,
                "verifier_sha256": super::terminal_fingerprint(&contract["verifier_commands"][index]).expect("grant digest"),
                "call_id": call, "source_postimages": sources
            });
        }
        (json!({"command_type": "focused_verifier", "command_line": json!({"verifier_index": index}).to_string(), "step": 1}),
            json!({"command_type": "focused_verifier", "step": 1, "success": passed,
                "error": if passed { serde_json::Value::Null } else { json!("test failed") }, "output": output}))
    }

    fn outcome_mutation() -> (serde_json::Value, serde_json::Value) {
        (json!({"command_type": "apply_patch", "command_line": "fixture patch", "step": 1}),
            json!({"command_type": "apply_patch", "step": 1, "success": true, "output": {"changes": ["src/example.rs"]}}))
    }

    fn outcome_guard_rejection() -> (serde_json::Value, serde_json::Value) {
        let done = terminal_done_result();
        let mut command = done.arguments["commands"][0].clone();
        command["step"] = json!(3);
        let mut guard = code_tools::command_run::CommandRunTerminalStatusGuard::default();
        guard.observe_step(Some(2));
        guard.observe_result(Some(false));
        (command, json!({"command_type": "task_status", "step": 3, "success": false,
            "error": guard.done_error(Some(3), true).expect("batch-local failure fence")}))
    }

    fn outcome_batch(runtime: &str, pairs: Vec<(serde_json::Value, serde_json::Value)>) -> serde_json::Value {
        let (commands, results): (Vec<_>, Vec<_>) = pairs.into_iter().unzip();
        let success = results.iter().all(|result| result["success"] == true);
        let error = results.iter().find(|result| result["success"] == false)
            .and_then(|result| result.get("error")).cloned().unwrap_or(serde_json::Value::Null);
        json!({"type": "tool_result", "tool_name": "command_run", "runtime_id": runtime,
            "success": success, "error": error, "input": {"commands": commands}, "output": {"results": results}})
    }

    fn append_outcome(session: &mut SessionManagement, runtime: &str, pairs: Vec<(serde_json::Value, serde_json::Value)>) {
        session.push_log(outcome_batch(runtime, pairs).to_string(), Utc::now());
    }

    fn streamed_outcome(session: &mut SessionManagement, runtime: &str, pair: (serde_json::Value, serde_json::Value)) {
        let (command, result) = &pair;
        let events = json!([
            {"status": "ready", "command_index": 0, "step": command["step"],
                "command_type": command["command_type"], "provider_tool_call_id": "provider-call"},
            {"status": "completed", "result_index": 0, "step": result["step"],
                "command_type": result["command_type"], "success": result["success"], "error": result["error"], "result": result}
        ]);
        for event in events.as_array().expect("events") {
            let mut event = event.clone();
            event["type"] = json!("streamed_command_event");
            event["runtime_id"] = json!(runtime);
            event["session_id"] = json!(session.session_id);
            session.push_log(event.to_string(), Utc::now());
        }
        let mut batch = outcome_batch(runtime, vec![pair]);
        batch["output"]["command_events"] = events;
        session.push_log(batch.to_string(), Utc::now());
    }

    fn native_streamed_batch(runtime: &RuntimeAggregate, provider_call: &str, mut pairs: Vec<(serde_json::Value, serde_json::Value)>) -> serde_json::Value {
        for (index, (command, result)) in pairs.iter_mut().enumerate() {
            command["command_id"] = json!(format!("{provider_call}:{index}"));
            command["command_run_id"] = json!(provider_call);
            command["provider_tool_call_id"] = json!(provider_call);
            command["command_index"] = json!(index);
            for key in ["command_id", "command_run_id", "provider_tool_call_id", "command_index"] {
                result[key] = command[key].clone();
            }
            result["command"] = command.clone();
        }
        let mut batch = outcome_batch(&runtime.runtime_id, pairs);
        let now = Utc::now();
        let events = batch["input"]["commands"].as_array().expect("commands").iter()
            .zip(batch["output"]["results"].as_array().expect("results")).enumerate()
            .flat_map(|(index, (command, result))| [
                crate::provider_flow::streamed_command_run::streamed_command_event_record(
                    "ready", &runtime.runtime_id, provider_call, index, command, None, now),
                crate::provider_flow::streamed_command_run::streamed_command_result_record(
                    "completed", &runtime.runtime_id, index, result, now),
            ]).collect::<Vec<_>>();
        batch["output"]["commands"] = batch["input"]["commands"].clone();
        batch["output"]["command_events"] = json!(events);
        batch
    }

    fn native_crossed_index_batch(runtime: &RuntimeAggregate) -> serde_json::Value {
        let done = terminal_done_result();
        let mut doing = (done.arguments["commands"][0].clone(), done.result["results"][0].clone());
        doing.0["command_line"] = json!(json!({"status": "doing"}).to_string());
        doing.1["output"]["task_status"]["status"] = json!("doing");
        let mut read = (json!({"command_type": "source_read", "step": 2,
            "command_line": json!({"path": "tests/test_graph_process.py", "start_line": 69,
                "end_line": 248, "line_numbers": true}).to_string()}),
            crate::tool_callback_sanitizer::tests::source_read_result());
        read.1["step"] = json!(2);
        let mut batch = native_streamed_batch(runtime, "scripted-command-100", vec![read, doing]);
        let events = batch["output"]["command_events"].as_array_mut().expect("events");
        events.swap(1, 2);
        events.swap(2, 3);
        events[2]["result_index"] = json!(0);
        events[3]["result_index"] = json!(1);
        batch
    }

    fn record_native_outcome(session: &mut SessionManagement, runtime: &RuntimeAggregate, batch: &serde_json::Value) {
        crate::tool_flow::command_run_result::record_streamed_command_events(session, runtime, &batch["output"]);
        crate::context::accumulate_tool_result_with_provider_metadata(
            session, "command_run", batch["input"].clone(), batch["output"].clone(),
            batch["success"].as_bool().expect("batch success"), batch["error"].as_str().map(str::to_owned),
            Some(&runtime.runtime_id), None,
        ).expect("native context accumulation");
    }

    fn terminal_done_result() -> ToolExecutionResult {
        ToolExecutionResult {
            tool_name: "command_run".to_string(),
            arguments: json!({"commands":[{"command_type":"task_status", "step":1,
                "command_line":json!({"status":"done"}).to_string()}]}),
            result: json!({"results":[{"command_type":"task_status", "step":1,
                "success":true, "output":{"task_status":{"status":"done"}}}]}),
            success: true,
            error: None,
        }
    }

    fn source_read_rejection_fixture() -> (tempfile::TempDir, SessionManagement, RuntimeAggregate, (serde_json::Value, serde_json::Value)) {
        use code_tools::commands::source_read::{SourceReadPreExecutionRejection, parse_command_line_typed};
        let root = tempfile::tempdir().expect("workspace");
        let mut session = outcome_session();
        session.session_directory = root.path().canonicalize().expect("physical workspace");
        std::fs::write(session.session_directory.join("answer.txt"), "answer\n").expect("source file");
        let contract = session.jspace_contract.as_mut().unwrap();
        contract["source_read"] = json!(true);
        contract["allowed_operations"] = json!(["read", "modify", "command"]);
        contract["denied_operations"] = json!([]);
        contract["read_scopes"] = json!(["answer.txt"]);
        let runtime = test_runtime_with_usage(&session, 0);
        let command = json!({"command_type":"source_read", "step":1,
            "command_line":r#"{"path":"answer.txt","start_line":1,"end_line":ninety}"#});
        let error = parse_command_line_typed(command["command_line"].as_str().unwrap()).unwrap_err();
        let mut command = command;
        command["command"] = json!("source_read");
        let proof = SourceReadPreExecutionRejection::new(&session.session_id, &runtime.runtime_id,
            "rejected-read", "rejected-read:step:1:index:0", "a".repeat(64).as_str(),
            &command, error.rejection_kind().unwrap(), error.to_string()).expect("router witness");
        let store = tura_path::command_receipts::ReceiptStore::open(&session.session_directory).expect("receipt store");
        proof.publish(&store).expect("durable router witness");
        (root, session, runtime, (command, proof.failed_result()))
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn typed_pre_execution_rejection_preserves_corrected_evidence_only_completion() {
        use code_tools::commands::source_read::{SourceReadHandler, open_admitted_root};
        use code_tools::runtime::tool::{ToolCall, ToolContext, ToolHandler, ToolPayload};
        for rejected in [false, true] {
            let (_root, mut session, runtime, pair) = source_read_rejection_fixture();
            if rejected {
                assert!(!super::terminal_command_outcome_proven(&pair.0, &pair.1));
                let batch = outcome_batch(&runtime.runtime_id, vec![pair]);
                record_native_outcome(&mut session, &runtime, &batch);
                assert_eq!(session.session_log.last().unwrap().value()["output"]["results"][0]["success"], false);
                assert_eq!(session.session_log.last().unwrap().value()["output"], batch["output"]);
            }
            let arguments = json!({"path":"answer.txt", "start_line":1, "end_line":1});
            let context = ToolContext::new(session.session_directory.clone())
                .with_call_id("corrected-read".to_owned())
                .with_source_read_root(Some(std::sync::Arc::new(open_admitted_root(&session.session_directory).unwrap())));
            let read = SourceReadHandler.handle(ToolCall { tool_name:"source_read".to_owned(),
                call_id:"corrected-read".to_owned(), payload:ToolPayload::Function { arguments:arguments.clone() },
            }, context).await.expect("actual corrected read");
            assert_eq!(read.success, Some(true));
            assert_eq!(read.body["stdout"], "answer\n");
            let batch = outcome_batch(&runtime.runtime_id, vec![(
                json!({"command_type":"source_read", "step":1, "command_line":arguments.to_string()}),
                json!({"command_type":"source_read", "step":1, "success":true, "output":read.body}),
            )]);
            record_native_outcome(&mut session, &runtime, &batch);
            let mutation = outcome_batch(&runtime.runtime_id, vec![outcome_mutation()]);
            record_native_outcome(&mut session, &runtime, &mutation);
            let verifier = outcome_batch(&runtime.runtime_id, vec![outcome_verifier(&session, "verified-call", 0, 0, true)]);
            record_native_outcome(&mut session, &runtime, &verifier);
            let done = terminal_done_result();
            let done_batch = outcome_batch(&runtime.runtime_id, vec![(done.arguments["commands"][0].clone(), done.result["results"][0].clone())]);
            record_native_outcome(&mut session, &runtime, &done_batch);
            let raw = session.session_log.iter().map(|entry| entry.raw().to_owned()).collect::<Vec<_>>();
            let tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(!tracker.hard_veto, "rejected={rejected}");
            assert_eq!(tracker.mutation_generation, 1);
            assert_eq!(tracker.delivery_status(&session, &runtime, std::slice::from_ref(&done)), Some("done"));
            assert_eq!(raw, session.session_log.iter().map(|entry| entry.raw().to_owned()).collect::<Vec<_>>(), "audit history is immutable");
            session.session_directory = session.session_directory.join("foreign-workspace");
            assert_eq!(tracker.delivery_status(&session, &runtime, &[done]), None, "receipt location is immutable");
        }
    }

    #[test]
    fn rejection_recovery_blocks_forged_changed_mixed_and_unperformed_outcomes() {
        for scenario in 0..12 {
            let (_root, mut session, runtime, (mut command, mut result)) = source_read_rejection_fixture();
            match scenario {
                0 => { result["output"].as_object_mut().unwrap().remove("pre_execution_rejection"); }
                1 => result["output"]["pre_execution_rejection"]["process_started"] = json!(true),
                2 => result["output"]["pre_execution_rejection"]["runtime_id"] = json!("foreign-runtime"),
                3 => result["output"]["pre_execution_rejection"]["command_sha256"] = json!("b".repeat(64)),
                4 => { result["success"] = json!(true); result["error"] = serde_json::Value::Null; }
                5 => result["output"]["outcome"] = json!("unknown"),
                6 => command["command_line"] = json!(r#"{"path":"answer.txt","start_line":1,"end_line":1}"#),
                7 => session.jspace_contract.as_mut().unwrap()["denied_operations"] = json!(["read"]),
                8 => {
                    use code_tools::commands::source_read::SourceReadPreExecutionRejection;
                    let proof = SourceReadPreExecutionRejection::parse_bounded(&result["output"]["pre_execution_rejection"]).unwrap();
                    let store = tura_path::command_receipts::ReceiptStore::open_existing(&session.session_directory).unwrap();
                    store.replace(&proof.receipt_name(), b"{}").expect("corrupt durable proof");
                }
                _ => {}
            }
            let mut pairs = vec![(command, result)];
            if scenario == 9 { pairs.push(outcome_mutation()); }
            let mut batch = outcome_batch(&runtime.runtime_id, pairs);
            if scenario == 10 { batch["input"]["execution_id"] = json!("foreign-execution"); }
            record_native_outcome(&mut session, &runtime, &batch);
            if scenario == 11 { record_native_outcome(&mut session, &runtime, &batch); }
            let mut terminal_runtime = test_runtime_with_usage(&session, 0);
            terminal_runtime.runtime_id = "terminal-runtime".to_owned();
            let done = terminal_done_result();
            let batch = outcome_batch(&terminal_runtime.runtime_id, vec![(done.arguments["commands"][0].clone(), done.result["results"][0].clone())]);
            record_native_outcome(&mut session, &terminal_runtime, &batch);
            let tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(tracker.hard_veto, "scenario {scenario}");
            assert_eq!(tracker.delivery_status(&session, &terminal_runtime, &[done]), Some("blocked"), "scenario {scenario}");
        }
    }

    #[test]
    fn rejection_witness_never_forgives_prior_effects_or_satisfies_a_failed_verifier() {
        for unknown in [false, true] {
            let (_root, mut session, runtime, pair) = source_read_rejection_fixture();
            let prior = if unknown {
                (json!({"command_type":"source_read", "step":1, "command_line":"fixture"}),
                    json!({"command_type":"source_read", "step":1, "success":false,
                        "error":"SOURCE_READ_ARGUMENTS_INVALID: forged prefix", "output":{"outcome":"unknown"}}))
            } else { outcome_verifier(&session, "failed-call", 0, 7, false) };
            let batch = outcome_batch(&runtime.runtime_id, vec![prior]);
            record_native_outcome(&mut session, &runtime, &batch);
            let batch = outcome_batch(&runtime.runtime_id, vec![pair]);
            record_native_outcome(&mut session, &runtime, &batch);
            let tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(!tracker.proven(), "no unperformed verification or unknown effect is excused");
            assert_eq!(tracker.hard_veto, unknown);
            if !unknown { assert_eq!(tracker.failures.get(&0), Some(&None)); }
        }
    }

    #[test]
    fn terminal_evidence_keeps_success_proof_and_exact_v1_keys() {
        let mut session = outcome_session();
        let runtime = test_runtime_with_usage(&session, 0);
        let done = terminal_done_result();
        append_outcome(&mut session, &runtime.runtime_id, vec![
            (done.arguments["commands"][0].clone(), done.result["results"][0].clone())]);
        let tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(tracker.proven());
        assert_eq!(tracker.delivery_status(&session, &runtime, &[done]), Some("done"));
        for terminal_status in ["done", "blocked"] {
            let marker = super::TerminalEvidenceRecord {
                kind: "nokiy.terminal_evidence", schema_version: "nokiy_terminal_evidence_v1",
                session_id: &session.session_id, runtime_id: &runtime.runtime_id,
                terminal_status, delivery_mode: "evidence_only",
                parent_acceptance_required: true, final_summary_turn_executed: false,
            };
            assert_eq!(serde_json::to_value(marker).expect("terminal marker"), json!({
                "type":"nokiy.terminal_evidence", "schema_version":"nokiy_terminal_evidence_v1",
                "session_id":session.session_id, "runtime_id":runtime.runtime_id, "terminal_status":terminal_status,
                "delivery_mode":"evidence_only", "parent_acceptance_required":true, "final_summary_turn_executed":false,
            }));
        }
    }

    #[test]
    fn blocked_terminal_delivery_preserves_failed_unknown_and_unresolved_history() {
        for scenario in 0..4 {
            let mut session = outcome_session();
            let past = if scenario == 0 {
                outcome_verifier(&session, "failed-verifier", 0, 7, false)
            } else {
                (json!({"command_type":"shell_command", "step":1, "command_line":"fixture"}),
                    json!({"command_type":"shell_command", "step":1, "success":scenario != 1,
                        "error":if scenario == 1 { json!("exit 7") } else { serde_json::Value::Null },
                        "output":match scenario {
                            1 => json!({"exit_code":7}),
                            2 => json!({"exit_code":0, "outcome":"unknown"}),
                            _ => json!({"exit_code":0, "terminal_receipt":{"reconcile_required":true}}),
                        }}))
            };
            append_outcome(&mut session, "past-runtime", vec![past]);
            let mut tracker = super::TerminalOutcomeTracker::new(&session);
            let runtime = test_runtime_with_usage(&session, 0);
            let done = terminal_done_result();
            append_outcome(&mut session, &runtime.runtime_id, vec![
                (done.arguments["commands"][0].clone(), done.result["results"][0].clone())]);
            tracker.observe_session(&session);
            let raw = session.session_log.iter().map(|entry| entry.raw().to_owned()).collect::<Vec<_>>();
            let failures = tracker.failures.clone();
            let hard_veto = tracker.hard_veto;
            assert!(!tracker.proven());
            assert_eq!(tracker.delivery_status(&session, &runtime, &[done]), Some("blocked"));
            assert!(!tracker.proven(), "delivery is not recovery or acceptance");
            assert_eq!(tracker.failures, failures);
            assert_eq!(tracker.hard_veto, hard_veto);
            assert_eq!(raw, session.session_log.iter().map(|entry| entry.raw().to_owned()).collect::<Vec<_>>());
        }
    }

    #[test]
    fn blocked_terminal_delivery_rejects_unsafe_current_batches_and_foreign_history() {
        for scenario in 0..7 {
            let mut session = outcome_session();
            let past = outcome_verifier(&session, "failed-verifier", 0, 7, false);
            append_outcome(&mut session, "past-runtime", vec![past]);
            let tracker = super::TerminalOutcomeTracker::new(&session);
            let runtime = test_runtime_with_usage(&session, 0);
            let done = terminal_done_result();
            let mut batch = outcome_batch(&runtime.runtime_id, vec![
                (done.arguments["commands"][0].clone(), done.result["results"][0].clone())]);
            match scenario {
                0 => batch["session_id"] = json!("foreign-session"),
                1 => batch["output"]["results"][0]["output"]["outcome"] = json!("unknown"),
                2 => batch["output"]["results"][0]["step"] = json!(2),
                3 => batch["runtime_id"] = json!("foreign-runtime"),
                4 => session.session_log_retention.omitted_entries = 1,
                5 => {
                    session.push_log(json!({"type":"nokiy.terminal_evidence",
                        "session_id":session.session_id, "runtime_id":"foreign-runtime"}).to_string(), Utc::now());
                }
                _ => session.jspace_contract = None,
            }
            session.push_log(batch.to_string(), Utc::now());
            assert_eq!(tracker.delivery_status(&session, &runtime, &[done]), None, "scenario {scenario}");
            assert!(!tracker.proven());
        }
    }

    #[test]
    fn terminal_evidence_boundary_keeps_delivery_terminal_identity_and_compaction_fences() {
        let mut session = test_session("terminal-boundary");
        let mut runtime = test_runtime_with_usage(&session, 0);
        let results = vec![terminal_done_result()];
        let boundary = |session: &SessionManagement, runtime: &RuntimeAggregate, delivery, status, compact, reply| {
            super::terminal_evidence_boundary_proven(delivery, session, runtime, &results, status, compact, reply)
        };
        assert!(boundary(&session, &runtime, true, Some("done"), false, true));
        assert!(!boundary(&session, &runtime, false, Some("done"), false, true),
            "absent/unproven transport, checkpoint or feed delivery cannot skip continuation");
        assert!(!boundary(&session, &runtime, true, Some("done"), false, false), "publication failure");
        assert!(!boundary(&session, &runtime, true, Some("done"), true, true), "pending compaction");
        for status in [None, Some("doing"), Some("question")] {
            assert!(!boundary(&session, &runtime, true, status, false, true));
        }
        runtime.session_id = "foreign-session".to_string();
        assert!(!boundary(&session, &runtime, true, Some("done"), false, true));
        runtime.session_id = session.session_id.clone();
        runtime.runtime_id.clear();
        assert!(!boundary(&session, &runtime, true, Some("done"), false, true));
        runtime.runtime_id = "current-runtime".to_string();
        session.context_tokens.limit = 1;
        session.context_tokens.input = 1;
        assert!(!boundary(&session, &runtime, true, Some("done"), false, true), "automatic compaction");
        assert!(!super::terminal_results_end_in_done(&[]));
        let mut unsafe_results = results;
        unsafe_results[0].result["results"].as_array_mut().expect("results").push(
            json!({"command_type":"shell_command", "success":true, "output":{"exit_code":0}}));
        assert!(!super::terminal_results_end_in_done(&unsafe_results), "done must be last");
    }

    #[test]
    fn focused_outcome_native_streamed_fail_patch_pass_and_late_mutation() {
        let mut session = outcome_session();
        let runtime = test_runtime_with_usage(&session, 0);
        let failed = outcome_verifier(&session, "failed-call", 0, 1, false);
        record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "failed-provider", vec![failed]));
        let failed_history = session.session_log.iter().map(|entry| entry.raw().to_owned()).collect::<Vec<_>>();
        let failure = session.session_log.last().expect("native failure").value();
        assert_eq!(failure["success"], false);
        assert_eq!(failure["error"], "test failed");
        assert_eq!(failure["context_cache"]["error"], "test failed");
        assert!(session.session_log[1].value().get("error").is_none());
        assert_eq!(session.session_log[1].value()["result"]["error"], "test failed");
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(!tracker.hard_veto);
        assert!(!tracker.proven());
        record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "repair-provider", vec![outcome_mutation()]));
        tracker.observe_session(&session);
        assert!(!tracker.hard_veto);
        assert!(!tracker.proven());
        let passed = outcome_verifier(&session, "passed-call", 0, 0, true);
        let proof = passed.1["output"]["verification_evidence"].clone();
        record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "passed-provider", vec![passed]));
        let recorded = session.session_log.last().expect("native pass").value();
        assert_eq!(recorded["output"]["results"][0]["output"]["verification_evidence"], proof);
        assert_eq!(recorded["output"]["command_events"]["omitted_from_record"], true);
        assert_eq!(recorded["output"]["command_events"]["count"], 2);
        let raw = session.session_log.iter().map(|entry| entry.raw().to_owned()).collect::<Vec<_>>();
        tracker.observe_session(&session);
        assert!(tracker.proven());
        assert_eq!(tracker.mutation_generation, 1);
        assert_eq!(tracker.receipts.len(), 2, "callbacks are not additional executions");
        assert_eq!(tracker.runtimes.len(), 3, "distinct calls share one native runtime");
        assert_eq!(raw[..failed_history.len()], failed_history);
        assert_eq!(session.session_log.iter().map(|entry| entry.raw().to_owned()).collect::<Vec<_>>(), raw);
        record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "late-provider", vec![outcome_mutation()]));
        tracker.observe_session(&session);
        assert!(!tracker.hard_veto);
        assert!(!tracker.proven(), "a later mutation invalidates the resolution");
    }

    #[test]
    fn focused_outcome_native_source_pages_are_canonical_but_real_differences_veto_done() {
        for changed in [false, true] {
            let mut session = outcome_session();
            let runtime = test_runtime_with_usage(&session, 0);
            let result = crate::tool_callback_sanitizer::tests::source_read_result();
            let command = json!({"command_type": "source_read", "step": 1,
                "command_line": json!({"path": "tests/test_graph_process.py", "start_line": 69,
                    "end_line": 248, "line_numbers": true}).to_string()});
            let mut batch = native_streamed_batch(&runtime, "source-provider", vec![(command, result)]);
            if changed {
                // Same byte count, identity and pagination; genuinely different
                // source text must still fail the exact outcome fingerprint.
                let stdout = batch["output"]["results"][0]["output"]["stdout"].as_str().unwrap();
                batch["output"]["results"][0]["output"]["stdout"] = json!(stdout.replacen("abcdef", "ABCDEF", 1));
            }
            record_native_outcome(&mut session, &runtime, &batch);
            let streamed = &session.session_log[1].value()["result"];
            let completed = &session.session_log.last().unwrap().value()["output"]["results"][0];
            for result in [streamed, completed] {
                assert_eq!(result["output"]["stdout"].as_str().unwrap().len(), 10_055);
                assert_eq!(result["output"]["next_line"], 249);
                assert!(!result["output"]["stdout"].as_str().unwrap().contains("Total output lines:"));
            }
            assert_eq!(super::terminal_fingerprint(streamed) == super::terminal_fingerprint(completed), !changed);
            let tracker = super::TerminalOutcomeTracker::new(&session);
            assert_eq!(tracker.hard_veto, changed);
            assert_eq!(tracker.proven(), !changed);
            let delivery = tracker.delivery_status(&session, &runtime, &[terminal_done_result()]);
            if changed {
                assert_ne!(delivery, Some("done"));
            } else {
                assert_eq!(delivery, Some("done"));
            }
        }
    }

    #[test]
    fn focused_outcome_native_streamed_omissions_cannot_hide_mismatches_or_gaps() {
        for scenario in 0..5 {
            let mut session = outcome_session();
            let runtime = test_runtime_with_usage(&session, 0);
            let passed = outcome_verifier(&session, "pass", 0, 0, true);
            let mut batch = native_streamed_batch(&runtime, "provider", vec![passed]);
            match scenario {
                0 => batch["output"]["results"][0]["output"]["stdout"] = json!("not the streamed result"),
                1 => batch["output"]["command_events"][1]["provider_tool_call_id"] = json!("other-provider"),
                2 => { batch["output"]["command_events"].as_array_mut().expect("events").remove(1); },
                3 => batch["output"]["command_events"][1]["error"] = json!("contradictory event error"),
                _ => batch["output"]["command_events"] = json!({
                    "omitted_from_record": true, "count": 2, "reason": super::COMMAND_EVENTS_OMISSION_REASON
                }),
            }
            record_native_outcome(&mut session, &runtime, &batch);
            assert!(super::TerminalOutcomeTracker::new(&session).hard_veto, "scenario {scenario}");
        }
    }

    #[test]
    fn focused_outcome_native_command_projection_still_vetoes_semantic_mismatches() {
        for (field, value) in [
            ("command_line", json!("different command")),
            ("command_type", json!("task_status")),
            ("step", json!(2)),
        ] {
            let mut session = outcome_session();
            let runtime = test_runtime_with_usage(&session, 0);
            let passed = outcome_verifier(&session, "pass", 0, 0, true);
            let mut batch = native_streamed_batch(&runtime, "provider", vec![passed]);
            assert_ne!(batch["input"]["commands"][0][field], value, "{field}");
            batch["output"]["commands"][0][field] = value;
            record_native_outcome(&mut session, &runtime, &batch);
            let tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(tracker.hard_veto, "{field}");
            assert!(!tracker.proven(), "{field}");
        }
    }

    #[test]
    fn focused_outcome_native_omission_marker_requires_exact_schema_and_retained_completions() {
        let mut session = outcome_session();
        let runtime = test_runtime_with_usage(&session, 0);
        let passed = outcome_verifier(&session, "pass", 0, 0, true);
        let batch = native_streamed_batch(&runtime, "provider", vec![passed]);
        let sanitized = crate::tool_callback_sanitizer::sanitize_tool_callback_output(&batch["output"]);
        let marker = &sanitized["command_events"];
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(!tracker.streamed_batch_matches(&runtime.runtime_id, 1, marker), "omission alone supplies no proof");
        crate::tool_flow::command_run_result::record_streamed_command_events(&mut session, &runtime, &batch["output"]);
        tracker.observe_session(&session);
        assert!(!tracker.hard_veto);
        assert!(!tracker.proven(), "completions still need their canonical batch");
        assert!(tracker.streamed_batch_matches(&runtime.runtime_id, 1, marker));
        for scenario in 0..5 {
            let mut malformed = marker.clone();
            match scenario {
                0 => { malformed.as_object_mut().expect("marker").remove("omitted_from_record"); malformed["unexpected"] = json!(true); },
                1 => malformed["reason"] = json!("arbitrary omission"),
                2 => malformed["count"] = json!(1),
                3 => malformed["unexpected"] = json!(true),
                _ => malformed["count"] = json!(0),
            }
            assert!(!tracker.streamed_batch_matches(&runtime.runtime_id, 1, &malformed), "scenario {scenario}");
        }
    }

    #[test]
    fn focused_outcome_native_distinct_sequences_do_not_replay_receipts_or_stream_calls() {
        for duplicate_provider in [false, true] {
            let mut session = outcome_session();
            let runtime = test_runtime_with_usage(&session, 0);
            let passed = outcome_verifier(&session, "first-call", 0, 0, true);
            record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "first-provider", vec![passed]));
            let mut tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(tracker.proven());
            let passed = outcome_verifier(&session, if duplicate_provider { "new-call" } else { "first-call" }, 0, 0, true);
            record_native_outcome(&mut session, &runtime, &native_streamed_batch(
                &runtime, if duplicate_provider { "first-provider" } else { "new-provider" }, vec![passed]));
            tracker.observe_session(&session);
            assert!(tracker.hard_veto);
            assert!(!tracker.proven());
        }
    }

    #[test]
    fn focused_outcome_native_advisory_context_does_not_mask_uncertain_failures() {
        for (key, value) in [
            ("outcome", json!("unknown")), ("process_reaped", json!(false)),
            ("failure_class", json!("cleanup")), ("authority_effect", json!("unproven")),
            ("authoritative_publication", json!("unknown")), ("exit_code", json!(-1)),
        ] {
            let mut session = outcome_session();
            let runtime = test_runtime_with_usage(&session, 0);
            let mut failed = outcome_verifier(&session, "uncertain-call", 0, 1, false);
            failed.1["output"]["terminal_receipt"][key] = value;
            record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "uncertain-provider", vec![failed]));
            let passed = outcome_verifier(&session, "pass", 0, 0, true);
            record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "passed-provider", vec![passed]));
            let tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(tracker.hard_veto, "{key}");
            assert!(!tracker.proven(), "{key}");
        }
    }

    #[test]
    fn focused_outcome_native_proofs_still_require_the_exact_grant_receipt_and_sources() {
        for scenario in 0..6 {
            let mut session = outcome_session();
            let runtime = test_runtime_with_usage(&session, 0);
            let failed = outcome_verifier(&session, "fail", 0, 1, false);
            record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "failed-provider", vec![failed]));
            let mut passed = outcome_verifier(&session, "pass", 0, 0, true);
            let proof = &mut passed.1["output"]["verification_evidence"];
            match scenario {
                0 => proof["authorization_semantic_sha256"] = json!("d".repeat(64)),
                1 => proof["verifier_sha256"] = json!("d".repeat(64)),
                2 => proof["verifier_index"] = json!(1),
                3 => proof["call_id"] = json!("other-call"),
                4 => { proof["source_postimages"].as_object_mut().expect("images").remove("src/example.rs"); },
                _ => proof["source_postimages"]["src/ungranted.rs"] = json!({"sha256": "b".repeat(64), "bytes": 28, "mode": 420}),
            }
            record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "passed-provider", vec![passed]));
            let tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(tracker.hard_veto, "scenario {scenario}");
            assert!(!tracker.proven(), "scenario {scenario}");
        }
    }

    #[test]
    fn focused_outcome_native_legacy_success_and_advisory_proof_do_not_resolve_failure() {
        let mut session = outcome_session();
        let runtime = test_runtime_with_usage(&session, 0);
        let legacy = outcome_verifier(&session, "initial-pass", 0, 0, false);
        record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "initial-provider", vec![legacy]));
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(tracker.proven(), "legacy success remains valid without resolving anything");
        let failed = outcome_verifier(&session, "fail", 0, 1, false);
        record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "failed-provider", vec![failed]));
        let proof = outcome_verifier(&session, "legacy-pass", 0, 0, true).1["output"]["verification_evidence"].clone();
        let mut legacy = outcome_verifier(&session, "legacy-pass", 0, 0, false);
        legacy.1["output"]["source_postimages"] = proof["source_postimages"].clone();
        legacy.1["output"]["stdout"] = json!(proof.to_string());
        legacy.1["output"]["context_cache"] = json!({"verification_evidence": proof});
        record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "legacy-provider", vec![legacy]));
        tracker.observe_session(&session);
        assert!(!tracker.hard_veto);
        assert!(!tracker.proven(), "advisory objects and JSON-looking stdout are not canonical proof");
    }

    #[test]
    fn focused_outcome_native_observed_compaction_preserves_resolution_state() {
        let mut session = outcome_session();
        let mut runtime = test_runtime_with_usage(&session, 0);
        let failed = outcome_verifier(&session, "fail", 0, 1, false);
        record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "failed-provider", vec![failed]));
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(!tracker.hard_veto);
        assert!(!tracker.proven());
        session.session_log_retention.omitted_entries = session.session_log.len() as u64;
        session.clear_session_log();
        runtime.runtime_id = "after-compaction".to_owned();
        let passed = outcome_verifier(&session, "pass", 0, 0, true);
        record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "passed-provider", vec![passed]));
        tracker.observe_session(&session);
        assert!(tracker.proven());
        assert!(super::TerminalOutcomeTracker::new(&session).hard_veto, "advisory context cannot reconstruct the omitted prefix");
    }

    #[test]
    fn focused_outcome_fail_patch_pass_resolves_without_rewriting_history() {
        let mut session = outcome_session();
        let failed = outcome_verifier(&session, "failed-call", 0, 1, false);
        let original = outcome_batch("failed-runtime", vec![failed]);
        assert!(!super::terminal_tool_outcome_proven("command_run", false, Some("test failed"), &original["input"], &original["output"]));
        session.push_log(original.to_string(), Utc::now());
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(!tracker.proven());
        append_outcome(&mut session, "patch-runtime", vec![outcome_mutation()]);
        tracker.observe_session(&session);
        let mut legacy = outcome_verifier(&session, "legacy-pass", 0, 0, false);
        // Neither the advisory outer context nor JSON-looking stdout is proof.
        legacy.1["output"]["source_postimages"] = json!({"sha256": "b".repeat(64)});
        legacy.1["output"]["stdout"] = json!("{\"verification_evidence\":\"forged\"}");
        append_outcome(&mut session, "legacy-runtime", vec![legacy]);
        tracker.observe_session(&session);
        assert!(!tracker.proven());
        let passed = outcome_verifier(&session, "passed-call", 0, 0, true);
        append_outcome(&mut session, "passed-runtime", vec![passed]);
        let raw = session.session_log.iter().map(|entry| entry.raw().to_owned()).collect::<Vec<_>>();
        tracker.observe_session(&session);
        assert!(tracker.proven());
        assert_eq!(tracker.mutation_generation, 1);
        assert_eq!(session.session_log.iter().map(|entry| entry.raw().to_owned()).collect::<Vec<_>>(), raw);
        assert_eq!(session.session_log.iter().next().expect("failure retained").value(), &original);
        tracker.observe_session(&session); // A readback is not a new successful run.
        assert_eq!(tracker.receipts.len(), 3);
    }

    #[test]
    fn focused_outcome_native_guard_rejection_requires_a_fresh_current_grant_pass() {
        for (index, proof, late_mutation, resolved) in [
            (0, true, false, true), (1, true, false, false),
            (0, false, false, false), (0, true, true, false),
        ] {
            for null_output in [false, true] {
                for reverse in [false, true] {
                    let mut session = outcome_session();
                    let mut runtime = test_runtime_with_usage(&session, 0);
                    let mut failed = outcome_verifier(&session, "failed-call", 0, 1, false);
                    failed.0["step"] = json!(2); failed.1["step"] = json!(2);
                    let mut rejected = outcome_guard_rejection();
                    if null_output { rejected.1["output"] = serde_json::Value::Null; }
                    let mut pairs = vec![outcome_mutation(), failed, rejected];
                    if reverse { pairs.swap(0, 1); }
                    let mut batch = native_streamed_batch(&runtime, "failed-provider", pairs);
                    batch["output"]["command_events"].as_array_mut().expect("events")
                        .sort_by_key(|event| event["step"].as_u64().expect("step"));
                    record_native_outcome(&mut session, &runtime, &batch);
                    let failed_history = session.session_log.iter().map(|entry| entry.raw().to_owned()).collect::<Vec<_>>();
                    let mut tracker = super::TerminalOutcomeTracker::new(&session);
                    assert!(!tracker.hard_veto);
                    assert!(!tracker.proven(), "a fenced done does not settle the failed verifier");
                    assert_eq!(tracker.failures.get(&0), Some(&None));

                    runtime.runtime_id = "repaired-runtime".to_owned();
                    let mut passed = outcome_verifier(&session, "passed-call", index, 0, proof);
                    passed.0["step"] = json!(2); passed.1["step"] = json!(2);
                    let mut mutation = outcome_mutation();
                    if late_mutation { mutation.0["step"] = json!(3); mutation.1["step"] = json!(3); }
                    let done = terminal_done_result();
                    let mut status = (done.arguments["commands"][0].clone(), done.result["results"][0].clone());
                    let done_step = if late_mutation { 4 } else { 3 };
                    status.0["step"] = json!(done_step); status.1["step"] = json!(done_step);
                    let mut pairs = vec![mutation, passed, status];
                    if reverse { pairs.swap(0, 1); }
                    let mut batch = native_streamed_batch(&runtime, "repair-provider", pairs);
                    batch["output"]["command_events"].as_array_mut().expect("events")
                        .sort_by_key(|event| event["step"].as_u64().expect("step"));
                    record_native_outcome(&mut session, &runtime, &batch);
                    let raw = session.session_log.iter().map(|entry| entry.raw().to_owned()).collect::<Vec<_>>();
                    tracker.observe_session(&session);
                    assert!(!tracker.hard_veto);
                    assert_eq!(tracker.proven(), resolved);
                    assert_eq!(tracker.mutation_generation, 2);
                    let resolution = (index == 0 && proof).then_some(if late_mutation { 1 } else { 2 });
                    assert_eq!(tracker.failures.get(&0), Some(&resolution));
                    assert_eq!(tracker.receipts.len(), 2);
                    assert_eq!(tracker.runtimes.len(), 2);
                    assert!(tracker.streamed.is_empty());
                    assert_eq!(&raw[..failed_history.len()], failed_history.as_slice());
                    assert_eq!(session.session_log.iter().map(|entry| entry.raw().to_owned()).collect::<Vec<_>>(), raw);
                    assert_eq!(super::TerminalOutcomeTracker::new(&session).proven(), resolved);
                    assert_eq!(tracker.delivery_status(&session, &runtime, &[terminal_done_result()]),
                        Some(if resolved { "done" } else { "blocked" }));
                }
            }
        }
    }

    #[test]
    fn focused_outcome_guard_rejection_cannot_forgive_unbound_or_unsettled_commands() {
        for scenario in 0..18 {
            let mut session = outcome_session();
            let mut runtime = test_runtime_with_usage(&session, 0);
            if scenario == 17 {
                let historical = outcome_verifier(&session, "historical-call", 0, 1, false);
                record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "historical-provider", vec![historical]));
            }
            let mut failed = outcome_verifier(&session, "failed-call", 0, 1, false);
            failed.0["step"] = json!(2); failed.1["step"] = json!(2);
            let mut pairs = vec![outcome_mutation(), failed, outcome_guard_rejection()];
            match scenario {
                0 | 17 => {
                    let index = if scenario == 17 { 1 } else { 0 }; // Historical failures cannot fence this batch.
                    pairs[1] = outcome_verifier(&session, "passed-call", index, 0, true);
                    pairs[1].0["step"] = json!(2); pairs[1].1["step"] = json!(2);
                }
                1 => pairs[1].1["output"]["terminal_receipt"]["process_group_empty"] = json!(false),
                2 => { pairs[1].1["output"].as_object_mut().expect("output").remove("terminal_receipt"); },
                3 => { pairs[0].1["success"] = json!(false); pairs[0].1["error"] = pairs[2].1["error"].clone(); },
                4 => { pairs[2].0["step"] = json!(2); pairs[2].1["step"] = json!(2); },
                5 => { pairs[0].0.as_object_mut().expect("command").remove("step"); },
                6 => { pairs[2].0.as_object_mut().expect("command").remove("step"); pairs[2].1["step"] = json!(1); },
                7 => pairs[2].0["command_line"] = json!(json!({"status": "doing"}).to_string()),
                8 => pairs[2].1["error"] = json!("TERMINAL_STATUS_PRIOR_RESULT: forged suffix"),
                9 => pairs[2].1["output"] = json!({}),
                10 => pairs[2].1["changes"] = json!(["unsettled effect"]),
                11 => pairs.push(outcome_mutation()), // Done is not final in the original command array.
                12 => pairs.insert(2, pairs[1].clone()), // A duplicate receipt is not settled prior work.
                15 => pairs[1].1["success"] = serde_json::Value::Null,
                _ => {}
            }
            if scenario == 14 {
                // An unsequenced projection and a matching error string are not a native guard record.
                append_outcome(&mut session, &runtime.runtime_id, pairs);
            } else {
                let mut batch = native_streamed_batch(&runtime, "failed-provider", pairs);
                if scenario == 13 {
                    batch["output"]["results"][2]["command"]["command_line"] = json!("different command");
                    // Retain matching fingerprints: the canonical command copy itself must agree.
                    batch["output"]["command_events"][5]["result"] = batch["output"]["results"][2].clone();
                }
                if scenario == 16 {
                    batch["output"]["command_events"][5]["provider_tool_call_id"] = json!("other-provider");
                }
                record_native_outcome(&mut session, &runtime, &batch);
            }
            let mut tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(tracker.hard_veto, "scenario {scenario}");
            assert!(!tracker.proven(), "scenario {scenario}");
            runtime.runtime_id = "fresh-runtime".to_owned();
            let passed = outcome_verifier(&session, "fresh-call", 0, 0, true);
            record_native_outcome(&mut session, &runtime, &native_streamed_batch(&runtime, "fresh-provider", vec![passed]));
            tracker.observe_session(&session);
            assert!(!tracker.proven(), "a later pass cannot hide scenario {scenario}");
        }
    }

    #[test]
    fn focused_outcome_later_mutation_or_failure_requires_a_fresh_pass() {
        let mut session = outcome_session();
        let failed = outcome_verifier(&session, "fail", 0, 2, false);
        append_outcome(&mut session, "r-fail", vec![failed]);
        let passed = outcome_verifier(&session, "pass", 0, 0, true);
        append_outcome(&mut session, "r-pass", vec![passed]);
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(tracker.proven());
        append_outcome(&mut session, "r-patch", vec![outcome_mutation()]);
        tracker.observe_session(&session);
        assert!(!tracker.proven());
        let mut passed = outcome_verifier(&session, "repass", 0, 0, true);
        passed.1["output"]["verification_evidence"]["source_postimages"]["src/example.rs"]["sha256"] = json!("c".repeat(64));
        append_outcome(&mut session, "r-repass", vec![passed]);
        tracker.observe_session(&session);
        assert!(tracker.proven());
        let failed = outcome_verifier(&session, "fail-again", 0, 3, false);
        append_outcome(&mut session, "r-fail-again", vec![failed]);
        tracker.observe_session(&session);
        assert!(!tracker.proven());
        let mut passed = outcome_verifier(&session, "pass-after-failure", 0, 0, true);
        passed.1["output"]["verification_evidence"]["source_postimages"]["src/example.rs"]["sha256"] = json!("c".repeat(64));
        append_outcome(&mut session, "r-pass-after-failure", vec![passed]);
        tracker.observe_session(&session);
        assert!(tracker.proven());
    }

    #[test]
    fn focused_outcome_rejects_malformed_or_unbound_proofs_permanently() {
        for (pointer, value) in [
            ("/schema_version", json!("other")),
            ("/authorization_semantic_sha256", json!("d".repeat(64))),
            ("/verifier_index", json!(1)), ("/verifier_index", json!(-1)),
            ("/verifier_sha256", json!("d".repeat(64))), ("/call_id", json!("other-call")),
            ("/source_postimages", json!({})),
            ("/source_postimages", json!({"src/other.rs": {"sha256": "b".repeat(64), "bytes": 28, "mode": 420}})),
            ("/source_postimages/src~1example.rs/sha256", json!("B".repeat(64))),
            ("/source_postimages/src~1example.rs/bytes", json!(-1)),
            ("/source_postimages/src~1example.rs/mode", json!(1.5)),
        ] {
            let mut session = outcome_session();
            let failed = outcome_verifier(&session, "fail", 0, 1, false);
            append_outcome(&mut session, "fail-runtime", vec![failed]);
            let mut passed = outcome_verifier(&session, "bad-pass", 0, 0, true);
            *passed.1["output"]["verification_evidence"].pointer_mut(pointer).expect("fixture pointer") = value;
            append_outcome(&mut session, "bad-runtime", vec![passed]);
            let mut tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(tracker.hard_veto, "{pointer}");
            let passed = outcome_verifier(&session, "good-pass", 0, 0, true);
            append_outcome(&mut session, "good-runtime", vec![passed]);
            tracker.observe_session(&session);
            assert!(!tracker.proven(), "{pointer}");
        }
        for malformed in [serde_json::Value::Null, json!({"unexpected": true})] {
            let mut session = outcome_session();
            let mut passed = outcome_verifier(&session, "malformed", 0, 0, true);
            passed.1["output"]["verification_evidence"] = malformed;
            append_outcome(&mut session, "malformed-runtime", vec![passed]);
            assert!(super::TerminalOutcomeTracker::new(&session).hard_veto);
        }
    }

    #[test]
    fn focused_outcome_requires_same_entire_immutable_grant() {
        let mut session = outcome_session();
        let failed = outcome_verifier(&session, "fail-0", 0, 1, false);
        append_outcome(&mut session, "fail-runtime", vec![failed]);
        let passed = outcome_verifier(&session, "pass-1", 1, 0, true);
        append_outcome(&mut session, "other-runtime", vec![passed]);
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(!tracker.proven());
        assert!(!tracker.hard_veto, "a different valid verifier does not resolve index zero");
        session.jspace_contract.as_mut().expect("contract")["verifier_commands"][0]["timeout_seconds"] = json!(31);
        let passed = outcome_verifier(&session, "changed-grant", 0, 0, true);
        append_outcome(&mut session, "changed-runtime", vec![passed]);
        tracker.observe_session(&session);
        assert!(tracker.hard_veto);
    }

    #[test]
    fn focused_outcome_uncertain_cleanup_and_nonworkload_failures_never_resolve() {
        for (key, value) in [
            ("outcome", json!("unknown")), ("terminal_state", json!("terminated")),
            ("process_reaped", json!(false)), ("process_group_empty", json!(false)),
            ("termination_proven", json!(false)), ("reconcile_required", json!(true)),
            ("exit_code", json!(-1)), ("exit_code", serde_json::Value::Null),
            ("failure_class", json!("cleanup")), ("termination_origin", json!("shell")),
            ("unresolved_effects", json!(["pending"])), ("error", json!("cleanup failed")),
            ("authority_effect", json!("unproven")),
            ("staging_authority", json!("unknown")), ("authoritative_publication", json!("unknown")),
            ("timed_out", json!(true)),
        ] {
            let mut session = outcome_session();
            let mut failed = outcome_verifier(&session, "uncertain", 0, 1, false);
            failed.1["output"]["terminal_receipt"][key] = value;
            append_outcome(&mut session, "uncertain-runtime", vec![failed]);
            let passed = outcome_verifier(&session, "good-pass", 0, 0, true);
            append_outcome(&mut session, "good-runtime", vec![passed]);
            let tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(tracker.hard_veto, "{key}");
            assert!(!tracker.proven(), "{key}");
        }
    }

    #[test]
    fn focused_outcome_duplicate_receipts_and_canonical_batches_are_not_new_passes() {
        for duplicate_runtime in [false, true] {
            let mut session = outcome_session();
            let passed = outcome_verifier(&session, "same-call", 0, 0, true);
            append_outcome(&mut session, "same-runtime", vec![passed]);
            let mut tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(tracker.proven());
            append_outcome(&mut session, "patch-runtime", vec![outcome_mutation()]);
            let passed = outcome_verifier(&session, if duplicate_runtime { "new-call" } else { "same-call" }, 0, 0, true);
            append_outcome(&mut session, if duplicate_runtime { "same-runtime" } else { "new-runtime" }, vec![passed]);
            tracker.observe_session(&session);
            assert!(tracker.hard_veto);
            assert!(!tracker.proven());
        }
    }

    #[test]
    fn focused_outcome_native_sequence_distinguishes_calls_in_one_runtime() {
        let mut session = outcome_session();
        for (call, exit) in [("first-call", 1), ("second-call", 0)] {
            let pair = outcome_verifier(&session, call, 0, exit, exit == 0);
            let value = outcome_batch("same-runtime", vec![pair]);
            crate::context::accumulate_tool_result_with_provider_metadata(
                &mut session, "command_run", value["input"].clone(), value["output"].clone(),
                exit == 0, if exit == 0 { None } else { Some("test failed".to_owned()) },
                Some("same-runtime"), None,
            ).expect("native context accumulation");
        }
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(!tracker.hard_veto);
        assert!(tracker.proven());
        assert_eq!(tracker.runtimes.len(), 2);
        let duplicate = session.session_log.last().expect("last result").raw().to_owned();
        session.push_log(duplicate, Utc::now());
        tracker.observe_session(&session);
        assert!(tracker.hard_veto, "replaying the same native sequence remains invalid");
    }

    #[test]
    fn focused_outcome_batch_steps_are_happens_before_not_array_order() {
        for (mutation_step, verifier_step, expected, hard) in [(1, 2, true, false), (1, 1, false, true), (2, 1, false, false)] {
            for reverse in [false, true] {
                let mut session = outcome_session();
                let failed = outcome_verifier(&session, "fail", 0, 1, false);
                append_outcome(&mut session, "fail-runtime", vec![failed]);
                let mut mutation = outcome_mutation();
                mutation.0["step"] = json!(mutation_step); mutation.1["step"] = json!(mutation_step);
                let mut passed = outcome_verifier(&session, "pass", 0, 0, true);
                passed.0["step"] = json!(verifier_step); passed.1["step"] = json!(verifier_step);
                let mut pairs = vec![mutation, passed];
                if reverse { pairs.reverse(); }
                append_outcome(&mut session, "batch-runtime", pairs);
                let tracker = super::TerminalOutcomeTracker::new(&session);
                assert_eq!(tracker.proven(), expected);
                assert_eq!(tracker.hard_veto, hard);
            }
        }
        let mut session = outcome_session();
        let failed = outcome_verifier(&session, "fail", 0, 1, false);
        append_outcome(&mut session, "fail-runtime", vec![failed]);
        let passed = outcome_verifier(&session, "pass", 0, 0, true);
        let mut mutation = outcome_mutation();
        mutation.0["step"] = json!(2); mutation.1["step"] = json!(2);
        append_outcome(&mut session, "batch-runtime", vec![passed, mutation]);
        let tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(!tracker.proven());
        assert!(!tracker.hard_veto);
    }

    #[test]
    fn focused_outcome_nonmonotonic_prefixes_are_proven() {
        for order in [vec![0, 1], vec![1, 0], vec![0, 1, 2], vec![1, 0, 2], vec![1, 2, 0]] {
            let mut session = outcome_session();
            let runtime = test_runtime_with_usage(&session, 0);
            let done = terminal_done_result();
            let mut doing = (done.arguments["commands"][0].clone(), done.result["results"][0].clone());
            doing.0["command_line"] = json!(json!({"status": "doing"}).to_string());
            doing.1["output"]["task_status"]["status"] = json!("doing");
            let mut read = (json!({"command_type": "source_read", "step": 2,
                "command_line": json!({"path": "tests/test_graph_process.py", "start_line": 69,
                    "end_line": 248, "line_numbers": true}).to_string()}),
                crate::tool_callback_sanitizer::tests::source_read_result());
            read.1["step"] = json!(2);
            let pairs = [doing.clone(), read, doing];
            let mut batch = native_streamed_batch(&runtime, "prefix-provider",
                order.iter().map(|&index| pairs[index].clone()).collect());
            batch["output"]["command_events"].as_array_mut().expect("events")
                .sort_by_key(|event| event["step"].as_u64().expect("step"));
            record_native_outcome(&mut session, &runtime, &batch);
            let mut tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(!tracker.hard_veto, "{order:?}");
            assert!(tracker.proven(), "{order:?}");
            assert_eq!(tracker.mutation_generation, 0);
            assert!(tracker.streamed.is_empty());
            let mut passed = outcome_verifier(&session, "pass", 0, 0, true);
            passed.0["step"] = json!(2); passed.1["step"] = json!(2);
            let done = terminal_done_result();
            let mut status = (done.arguments["commands"][0].clone(), done.result["results"][0].clone());
            status.0["step"] = json!(3); status.1["step"] = json!(3);
            record_native_outcome(&mut session, &runtime,
                &native_streamed_batch(&runtime, "repair-provider", vec![outcome_mutation(), passed, status]));
            tracker.observe_session(&session);
            assert!(!tracker.hard_veto, "{order:?}");
            assert_eq!(tracker.mutation_generation, 1);
            assert_eq!(tracker.delivery_status(&session, &runtime, &[terminal_done_result()]), Some("done"), "{order:?}");
        }
    }

    #[test]
    fn focused_outcome_native_crossed_completion_indices_keep_command_order() {
        let mut session = outcome_session();
        let runtime = test_runtime_with_usage(&session, 0);
        let batch = native_crossed_index_batch(&runtime);
        assert_eq!(batch["output"]["commands"], batch["input"]["commands"]);
        let events = batch["output"]["command_events"].as_array().expect("events");
        for (index, kind, step) in [(0, "source_read", 2), (1, "task_status", 1)] {
            assert_eq!(events[index]["status"], "ready");
            assert_eq!(events[index]["command_index"], json!(index));
            for value in [&events[index], &batch["input"]["commands"][index], &batch["output"]["results"][index]] {
                assert_eq!(value["command_type"], kind);
                assert_eq!(value["step"], json!(step));
            }
        }
        for (event_index, command_index, result_index) in [(2, 1, 0), (3, 0, 1)] {
            let event = &events[event_index];
            assert_eq!(event["status"], "completed");
            assert_eq!(event["result_index"], json!(result_index));
            assert_eq!(event["provider_tool_call_id"], "scripted-command-100");
            assert_eq!(event["success"], true);
            for value in [event, &event["result"], &event["result"]["command"]] {
                assert_eq!(value["command_index"], json!(command_index));
            }
            assert_eq!(event["result"], batch["output"]["results"][command_index]);
        }
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        for (index, event) in events.iter().enumerate() {
            tracker.observe_streamed(event);
            assert!(!tracker.hard_veto, "event_index={index}");
        }
        assert!(!tracker.proven(), "completions still require their canonical batch");
        assert!(tracker.streamed_batch_matches(&runtime.runtime_id, 2, &batch["output"]["command_events"]));
        let mut conflicting = batch["output"]["command_events"].clone();
        conflicting[2]["result_index"] = json!(1);
        conflicting[3]["result_index"] = json!(0);
        assert!(!tracker.streamed_batch_matches(&runtime.runtime_id, 2, &conflicting),
            "duplicate event payloads must retain authenticated completion positions");
        tracker.observe_batch(&batch);
        assert!(tracker.proven());
        assert_eq!(tracker.mutation_generation, 0);
        assert!(tracker.streamed.is_empty());
        // Exercise native recording's exact omission marker as well as the
        // explicit command_events array above, without reordering final results.
        record_native_outcome(&mut session, &runtime, &batch);
        let native = super::TerminalOutcomeTracker::new(&session);
        assert!(!native.hard_veto);
        assert!(native.proven());
    }

    #[test]
    fn focused_outcome_streamed_indices_preserve_legacy_and_validate_every_copy() {
        assert_eq!(super::terminal_stream_indices(&json!({"status": "ready", "command_index": 0})), Some((0, None)));
        assert_eq!(super::terminal_stream_indices(&json!({"status": "ready", "command_index": 0, "result_index": 0})),
            Some((0, Some(0))));
        assert_eq!(super::terminal_stream_indices(&json!({"status": "completed", "result_index": 0})), Some((0, Some(0))));
        let completed = json!({"status": "completed", "command_index": 1, "result_index": 0,
            "result": {"command_index": 1, "command": {"command_index": 1}}});
        assert_eq!(super::terminal_stream_indices(&completed), Some((1, Some(0))));
        let mut embedded_only = completed.clone();
        embedded_only.as_object_mut().unwrap().remove("command_index");
        assert_eq!(super::terminal_stream_indices(&embedded_only), Some((1, Some(0))));
        embedded_only["result"].as_object_mut().unwrap().remove("command_index");
        assert_eq!(super::terminal_stream_indices(&embedded_only), Some((1, Some(0))));
        for field in ["/command_index", "/result/command_index", "/result/command/command_index"] {
            for value in [json!(0), json!(null), json!(-1), json!(0.5), json!("1"), json!(true),
                json!(super::MAX_TERMINAL_TRACKED_COMMANDS), json!(u64::MAX)] {
                let mut malformed = completed.clone();
                *malformed.pointer_mut(field).expect("identity copy") = value;
                assert!(super::terminal_stream_indices(&malformed).is_none(), "{field}: {malformed}");
            }
        }
        for value in [json!(null), json!(-1), json!(0.5), json!("0"), json!(true),
            json!(super::MAX_TERMINAL_TRACKED_COMMANDS), json!(u64::MAX)] {
            let mut malformed = completed.clone();
            malformed["result_index"] = value;
            assert!(super::terminal_stream_indices(&malformed).is_none(), "{malformed}");
        }
        for malformed in [
            json!({"status": "ready", "result_index": 0, "command": {"command_index": 0}}),
            json!({"status": "ready", "command_index": 0, "result_index": 1}),
            json!({"status": "completed", "command_index": 1}),
            json!({"status": "pending", "command_index": 1, "result_index": 0}),
        ] {
            assert!(super::terminal_stream_indices(&malformed).is_none(), "{malformed}");
        }
    }

    #[test]
    fn focused_outcome_crossed_indices_still_veto_conflicts_duplicates_gaps_and_final_mismatches() {
        for scenario in 0..15 {
            let mut session = outcome_session();
            let runtime = test_runtime_with_usage(&session, 0);
            let mut batch = native_crossed_index_batch(&runtime);
            let events = batch["output"]["command_events"].as_array_mut().expect("events");
            match scenario {
                0 => events[2]["command_index"] = json!(0),
                1 => events[2]["result"]["command_index"] = json!(0),
                2 => events[2]["result"]["command"]["command_index"] = json!(0),
                3 => events[2]["result"]["provider_tool_call_id"] = json!("other-provider"),
                4 => events[2]["result"]["command"]["provider_tool_call_id"] = json!("other-provider"),
                5 => events[2]["runtime_id"] = json!("other-runtime"),
                6 => events[2]["provider_tool_call_id"] = json!("other-provider"),
                7 => events[2]["result"]["runtime_id"] = json!("other-runtime"),
                8 => events[2]["result"]["command"]["runtime_id"] = json!("other-runtime"),
                9 => events[3]["result_index"] = json!(0),
                10 => events[3]["result_index"] = json!(2),
                11 => { let duplicate = events[2].clone(); events.push(duplicate); },
                12 => { events.remove(3); },
                13 => batch["output"]["results"].as_array_mut().expect("results").swap(0, 1),
                _ => batch["output"]["results"][0]["output"]["stdout"] = json!("not the streamed result"),
            }
            let mut streamed = super::TerminalOutcomeTracker::new(&session);
            for event in batch["output"]["command_events"].as_array().expect("events") {
                streamed.observe_streamed(event);
                if streamed.hard_veto { break; }
            }
            // Identity conflicts and duplicate positions must fail before the
            // final fingerprint comparison can incidentally reject them.
            assert_eq!(streamed.hard_veto, matches!(scenario, 0..=9 | 11), "scenario {scenario}");
            if !streamed.hard_veto { streamed.observe_batch(&batch); }
            assert!(streamed.hard_veto, "explicit events: scenario {scenario}");
            assert!(!streamed.proven(), "explicit events: scenario {scenario}");
            record_native_outcome(&mut session, &runtime, &batch);
            let tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(tracker.hard_veto, "scenario {scenario}");
            assert!(!tracker.proven(), "scenario {scenario}");
        }
    }

    #[test]
    fn focused_outcome_native_nonmonotonic_repair_preserves_streamed_indices() {
        for reverse in [false, true] {
            let mut session = outcome_session();
            let runtime = test_runtime_with_usage(&session, 0);
            let failed = outcome_verifier(&session, "fail", 0, 1, false);
            record_native_outcome(&mut session, &runtime,
                &native_streamed_batch(&runtime, "failed-provider", vec![failed]));
            let mut tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(!tracker.hard_veto);
            assert!(!tracker.proven());
            let mut passed = outcome_verifier(&session, "pass", 0, 0, true);
            passed.0["step"] = json!(2); passed.1["step"] = json!(2);
            let done = terminal_done_result();
            let mut status = (done.arguments["commands"][0].clone(), done.result["results"][0].clone());
            status.0["step"] = json!(3); status.1["step"] = json!(3);
            let mut pairs = vec![outcome_mutation(), passed, status];
            if reverse { pairs.swap(0, 1); }
            let mut batch = native_streamed_batch(&runtime, "repair-provider", pairs);
            batch["output"]["command_events"].as_array_mut().expect("events")
                .sort_by_key(|event| event["step"].as_u64().expect("step"));
            record_native_outcome(&mut session, &runtime, &batch);
            tracker.observe_session(&session);
            assert!(!tracker.hard_veto, "reverse {reverse}");
            assert!(tracker.proven(), "reverse {reverse}");
            assert_eq!(tracker.mutation_generation, 1);
            assert_eq!(tracker.failures.get(&0), Some(&Some(1)));
            assert_eq!(tracker.receipts.len(), 2);
            assert!(tracker.streamed.is_empty());
            assert_eq!(tracker.delivery_status(&session, &runtime, &[terminal_done_result()]), Some("done"));
        }
    }

    #[test]
    fn focused_outcome_nonmonotonic_verifier_chronology_is_not_array_order() {
        for (fail_step, mutation_step, pass_step, resolution) in [
            (1, 2, 3, Some(1)), (3, 2, 1, None), (1, 3, 2, Some(0)),
        ] {
            for order in [[0, 1, 2], [0, 2, 1], [1, 0, 2], [1, 2, 0], [2, 0, 1], [2, 1, 0]] {
                let mut session = outcome_session();
                let mut failed = outcome_verifier(&session, "fail", 0, 1, false);
                failed.0["step"] = json!(fail_step); failed.1["step"] = json!(fail_step);
                let mut mutation = outcome_mutation();
                mutation.0["step"] = json!(mutation_step); mutation.1["step"] = json!(mutation_step);
                let mut passed = outcome_verifier(&session, "pass", 0, 0, true);
                passed.0["step"] = json!(pass_step); passed.1["step"] = json!(pass_step);
                let pairs = [failed, mutation, passed];
                append_outcome(&mut session, "batch-runtime",
                    order.iter().map(|&index| pairs[index].clone()).collect());
                let tracker = super::TerminalOutcomeTracker::new(&session);
                assert!(!tracker.hard_veto, "{order:?}");
                assert_eq!(tracker.mutation_generation, 1);
                assert_eq!(tracker.failures.get(&0), Some(&resolution), "{order:?}");
                assert_eq!(tracker.proven(), resolution == Some(1), "{order:?}");
            }
        }
    }

    #[test]
    fn focused_outcome_nonmonotonic_batches_still_veto_mismatches_and_incomplete_effects() {
        for scenario in 0..9 {
            let mut session = outcome_session();
            let runtime = test_runtime_with_usage(&session, 0);
            let mut passed = outcome_verifier(&session, "pass", 0, 0, true);
            passed.0["step"] = json!(2); passed.1["step"] = json!(2);
            let mut mutation = outcome_mutation();
            match scenario {
                2 => { mutation.1.as_object_mut().expect("result").remove("output"); },
                3 => mutation.1["output"]["status"] = json!("pending"),
                4 => {
                    mutation.1["success"] = json!(false);
                    mutation.1["error"] = json!("effect failed");
                }
                _ => {}
            }
            let mut batch = native_streamed_batch(&runtime, "provider", vec![passed, mutation]);
            match scenario {
                0 => batch["output"]["results"].as_array_mut().expect("results").swap(0, 1),
                1 => batch["output"]["results"][1]["step"] = json!(2),
                5 => batch["output"]["command_events"][1]["provider_tool_call_id"] = json!("other-provider"),
                6 => { batch["output"]["command_events"].as_array_mut().expect("events").remove(1); },
                // A distinct completion index is valid; a batch-out-of-bounds
                // completion index is not (duplicates are covered separately).
                7 => batch["output"]["command_events"][3]["result_index"] = json!(2),
                8 => batch["output"]["command_events"][0]["command_index"] = json!(1),
                _ => {}
            }
            record_native_outcome(&mut session, &runtime, &batch);
            let tracker = super::TerminalOutcomeTracker::new(&session);
            assert!(tracker.hard_veto, "scenario {scenario}");
            assert!(!tracker.proven(), "scenario {scenario}");
        }
    }

    #[test]
    #[ignore = "offline diagnostic; requires NOKIY_TERMINAL_HISTORY_FIXTURE"]
    fn focused_outcome_saved_history_replay() {
        #[derive(serde::Deserialize)]
        struct SavedHistoryRecord { record_json: String }

        let path = std::env::var_os("NOKIY_TERMINAL_HISTORY_FIXTURE")
            .expect("set NOKIY_TERMINAL_HISTORY_FIXTURE to the saved native history");
        let fixture = std::fs::read(path).expect("read saved history fixture");
        let history: Vec<SavedHistoryRecord> = serde_json::from_slice(&fixture).expect("parse saved history array");
        assert!(!history.is_empty(), "saved history is empty");
        let session = outcome_session();
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        let mut batches = 0;
        for (index, entry) in history.into_iter().enumerate() {
            let record: serde_json::Value = serde_json::from_str(&entry.record_json).expect("parse exact native record");
            assert!(record.is_object(), "native history record must be an object");
            match record["type"].as_str() {
                Some("streamed_command_event") => tracker.observe_streamed(&record),
                Some("tool_result") => { tracker.observe_batch(&record); batches += 1; }
                _ => {}
            }
            assert!(!tracker.hard_veto, "first hard veto: event_index={index} runtime={} sequence={:?}",
                super::terminal_identity(&record["runtime_id"]).unwrap_or("<missing>"), record["sequence"].as_u64());
        }
        assert!(batches != 0 && tracker.proven(), "full saved history not proven");
    }

    #[test]
    fn focused_outcome_opaque_unknown_and_incomplete_batches_keep_hard_veto() {
        for scenario in 0..7 {
            let mut session = outcome_session();
            let failed = outcome_verifier(&session, "fail", 0, 1, false);
            let mut batch = outcome_batch("bad-runtime", vec![failed]);
            match scenario {
                0 => batch["tool_name"] = json!("opaque"),
                1 => batch["output"]["results"] = json!([]),
                2 => batch["output"]["results"][0]["success"] = serde_json::Value::Null,
                3 => batch["output"]["cancelled"] = json!(true),
                4 => { batch["input"]["commands"][0]["command_type"] = json!("shell_command"); batch["output"]["results"][0]["command_type"] = json!("shell_command"); }
                5 => batch["error"] = json!("unrelated wrapper failure"),
                _ => batch["output"]["results"][0]["reconcile_required"] = json!(true),
            }
            session.push_log(batch.to_string(), Utc::now());
            let passed = outcome_verifier(&session, "pass", 0, 0, true);
            append_outcome(&mut session, "pass-runtime", vec![passed]);
            assert!(super::TerminalOutcomeTracker::new(&session).hard_veto, "scenario {scenario}");
        }
    }

    #[test]
    fn focused_outcome_streamed_events_resolve_only_with_one_matching_canonical_result() {
        let mut session = outcome_session();
        let failed = outcome_verifier(&session, "fail", 0, 1, false);
        streamed_outcome(&mut session, "fail-runtime", failed);
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(!tracker.hard_veto);
        assert!(!tracker.proven());
        assert_eq!(tracker.receipts.len(), 1);
        let passed = outcome_verifier(&session, "pass", 0, 0, true);
        streamed_outcome(&mut session, "pass-runtime", passed);
        tracker.observe_session(&session);
        assert!(tracker.proven());
        assert_eq!(tracker.receipts.len(), 2, "callbacks are not additional runs");
        assert_eq!(tracker.runtimes.len(), 2);
    }

    #[test]
    fn focused_outcome_streamed_identity_duplicates_and_gaps_are_hard_vetoes() {
        for scenario in 0..8 {
            let mut session = outcome_session();
            let passed = outcome_verifier(&session, "pass", 0, 0, true);
            streamed_outcome(&mut session, "pass-runtime", passed);
            let mut entries = session.session_log.iter().map(|entry| entry.value().clone()).collect::<Vec<_>>();
            match scenario {
                // Legacy result-index-only identity still needs a matching ready.
                0 => entries[1]["result_index"] = json!(1),
                1 => entries[1]["runtime_id"] = json!("other-runtime"),
                2 => entries[1]["result"]["output"]["terminal_receipt"]["call_id"] = json!("other-call"),
                3 => entries[1]["provider_tool_call_id"] = json!("other-provider-call"),
                4 => entries.insert(1, entries[0].clone()),
                5 => { entries.remove(1); },
                6 => entries[1]["success"] = json!(false),
                _ => entries[2]["output"]["command_events"][1]["result"]["output"]["terminal_receipt"]["call_id"] = json!("other-call"),
            }
            session.replace_session_log(entries.into_iter().map(|value| value.to_string()));
            assert!(super::TerminalOutcomeTracker::new(&session).hard_veto, "scenario {scenario}");
        }
    }

    #[test]
    fn focused_outcome_compaction_retains_failures_but_cannot_reconstruct_missing_history() {
        let mut session = outcome_session();
        let failed = outcome_verifier(&session, "fail", 0, 1, false);
        append_outcome(&mut session, "fail-runtime", vec![failed]);
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        session.session_log_retention.omitted_entries = session.session_log.len() as u64;
        session.clear_session_log(); // Only already-observed entries are trimmed.
        let passed = outcome_verifier(&session, "pass", 0, 0, true);
        append_outcome(&mut session, "pass-runtime", vec![passed]);
        tracker.observe_session(&session);
        assert!(tracker.proven());
        assert!(super::TerminalOutcomeTracker::new(&session).hard_veto, "fresh readers lack the omitted prefix");
        append_outcome(&mut session, "unseen-patch", vec![outcome_mutation()]);
        session.session_log_retention.omitted_entries += session.session_log.len() as u64;
        session.clear_session_log();
        tracker.observe_session(&session);
        assert!(tracker.hard_veto, "trimming an unobserved outcome is a history gap");
    }

    #[test]
    fn focused_outcome_terminal_markers_and_inconsistent_postimages_are_hard_vetoes() {
        for marker in [json!({"type": "nokiy.terminal_evidence"}), json!({"schema_version": "nokiy_terminal_evidence_v1"})] {
            let mut session = outcome_session();
            session.push_log(marker.to_string(), Utc::now());
            assert!(super::TerminalOutcomeTracker::new(&session).hard_veto);
        }
        let mut session = outcome_session();
        let passed = outcome_verifier(&session, "pass-0", 0, 0, true);
        append_outcome(&mut session, "runtime-0", vec![passed]);
        let mut passed = outcome_verifier(&session, "pass-1", 1, 0, true);
        passed.1["output"]["verification_evidence"]["source_postimages"]["src/example.rs"]["sha256"] = json!("c".repeat(64));
        append_outcome(&mut session, "runtime-1", vec![passed]);
        assert!(super::TerminalOutcomeTracker::new(&session).hard_veto);
    }

    #[test]
    fn focused_outcome_readonly_empty_coverage_is_valid_but_mutable_empty_coverage_is_not() {
        let mut session = outcome_session();
        let contract = session.jspace_contract.as_mut().expect("contract");
        contract["declared_targets"] = json!([]); contract["write_scopes"] = json!([]);
        let failed = outcome_verifier(&session, "fail", 0, 1, false);
        append_outcome(&mut session, "fail-runtime", vec![failed]);
        let passed = outcome_verifier(&session, "pass", 0, 0, true);
        append_outcome(&mut session, "pass-runtime", vec![passed]);
        assert!(super::TerminalOutcomeTracker::new(&session).proven());
        session.jspace_contract.as_mut().expect("contract")["write_scopes"] = json!(["src/example.rs"]);
        assert!(super::TerminalOutcomeTracker::new(&session).hard_veto);
    }

    #[test]
    fn focused_outcome_storage_is_bounded_and_overflow_cannot_forget_a_veto() {
        let mut session = outcome_session();
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        for index in 0..=super::MAX_TERMINAL_TRACKED_COMMANDS {
            let passed = outcome_verifier(&session, &format!("call-{index}"), 0, 0, false);
            append_outcome(&mut session, &format!("runtime-{index}"), vec![passed]);
            tracker.observe_session(&session);
            session.session_log_retention.omitted_entries += session.session_log.len() as u64;
            session.clear_session_log();
        }
        assert!(tracker.hard_veto);
        assert!(!tracker.proven());
        assert!(tracker.receipts.len() <= super::MAX_TERMINAL_TRACKED_COMMANDS);
        assert!(tracker.runtimes.len() <= super::MAX_TERMINAL_TRACKED_COMMANDS);
        assert!(tracker.streamed.len() <= super::MAX_TERMINAL_TRACKED_COMMANDS);
        assert!(tracker.streamed_calls.len() <= super::MAX_TERMINAL_TRACKED_COMMANDS);
        assert!(super::terminal_fingerprint(&json!("x".repeat(super::MAX_TERMINAL_EVIDENCE_BYTES))).is_none());
    }

    #[test]
    fn focused_outcome_grant_hash_is_sorted_compact_utf8_and_includes_all_fields() {
        use sha2::{Digest, Sha256};
        let value = json!({"z": [false, 2], "a": {"b": "雪", "a": 1}});
        let expected = format!("{:x}", Sha256::digest("{\"a\":{\"a\":1,\"b\":\"雪\"},\"z\":[false,2]}".as_bytes()));
        assert_eq!(super::terminal_fingerprint(&value).as_deref(), Some(expected.as_str()));
        let mut with_extra = value.clone();
        with_extra["content_sha256"] = json!("c".repeat(64));
        assert_ne!(super::terminal_fingerprint(&value), super::terminal_fingerprint(&with_extra));
    }

    #[tokio::test]
    async fn focused_outcome_accepts_actual_remote_receipts_not_just_fixture_booleans() {
        use code_tools::runtime::tool::ToolContext;
        use code_tools::shell_executor::{VerifierObservation, execute_remote_focused_verifier};
        use std::sync::Arc;

        let root = tempfile::tempdir().expect("receipt workspace");
        let directory = root.path().canonicalize().expect("canonical workspace");
        let store = Arc::new(tura_path::command_receipts::ReceiptStore::open(&directory).expect("receipt store"));
        let mut session = outcome_session();
        for (call, exit) in [("actual-failure", 1), ("actual-pass", 0)] {
            let (command, fixture) = outcome_verifier(&session, call, 0, exit, exit == 0);
            let ctx = ToolContext::new(directory.clone()).with_call_id(call.to_owned())
                .with_receipt_store(Some(Arc::clone(&store)));
            let observation = VerifierObservation {
                success: exit == 0, exit_code: exit, stdout: "verifier output".to_owned(),
                stderr: if exit == 0 { String::new() } else { "test failed".to_owned() },
                process_reaped: true, process_group_empty: true, outcome: "known".to_owned(),
                source_postimages: None, verification_evidence: fixture["output"].get("verification_evidence").cloned(),
            };
            let response = execute_remote_focused_verifier(5, &ctx, async { Ok(observation) }).await;
            assert_eq!(response.success, exit == 0);
            assert!(response.changes.is_empty());
            let result = json!({"command_type": "focused_verifier", "step": 1, "success": response.success,
                "error": if response.success { serde_json::Value::Null } else { json!(response.stderr) }, "output": response.output});
            append_outcome(&mut session, call, vec![(command, result)]);
        }
        let tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(!tracker.hard_veto);
        assert!(tracker.proven());
        assert_eq!(tracker.receipts.len(), 2);
    }

    #[test]
    fn focused_outcome_selection_and_receipt_identity_must_be_bound() {
        for scenario in 0..9 {
            let mut session = outcome_session();
            let mut failed = outcome_verifier(&session, "fail", 0, 1, false);
            match scenario {
                0 => failed.0["command_line"] = json!("{\"verifier_index\":0,\"argv\":[]}"),
                1 => failed.0["command_line"] = json!("{\"verifier_index\":0,\"verifier_index\":0}"),
                2 => failed.0["command_line"] = json!("{\"verifier_index\":999}"),
                3 => failed.0["workdir"] = json!("."),
                4 => failed.1["output"]["terminal_receipt"]["call_id"] = json!(""),
                5 => failed.1["output"]["executor"] = json!("other"),
                6 => { failed.0["step"] = json!(0); failed.1["step"] = json!(0); },
                7 => session.jspace_contract = None,
                _ => failed.1["output"]["verification_evidence"] = json!({}),
            }
            append_outcome(&mut session, "failed-runtime", vec![failed]);
            assert!(super::TerminalOutcomeTracker::new(&session).hard_veto, "scenario {scenario}");
        }
    }

    #[test]
    fn focused_outcome_full_source_schema_and_coverage_are_required() {
        for scenario in 0..5 {
            let mut session = outcome_session();
            let contract = session.jspace_contract.as_mut().expect("contract");
            contract["declared_targets"] = json!(["src/example.rs", "src/second.rs"]);
            contract["write_scopes"] = contract["declared_targets"].clone();
            let mut passed = outcome_verifier(&session, "pass", 0, 0, true);
            let proof = &mut passed.1["output"]["verification_evidence"];
            match scenario {
                0 => { proof["source_postimages"].as_object_mut().expect("images").remove("src/second.rs"); },
                1 => proof["unexpected"] = json!(true),
                2 => proof["source_postimages"]["src/second.rs"]["unexpected"] = json!(true),
                3 => { proof.as_object_mut().expect("proof").remove("call_id"); },
                _ => proof["call_id"] = json!("x".repeat(super::MAX_TERMINAL_EVIDENCE_BYTES)),
            }
            append_outcome(&mut session, "passed-runtime", vec![passed]);
            assert!(super::TerminalOutcomeTracker::new(&session).hard_veto, "scenario {scenario}");
        }
    }

    #[test]
    fn focused_outcome_pending_stream_bindings_survive_observed_compaction() {
        let mut session = outcome_session();
        let passed = outcome_verifier(&session, "pass", 0, 0, true);
        streamed_outcome(&mut session, "passed-runtime", passed);
        let entries = session.session_log.iter().map(|entry| entry.raw().to_owned()).collect::<Vec<_>>();
        session.replace_session_log(entries[..2].iter().cloned());
        let mut tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(!tracker.proven(), "callbacks alone are not a canonical successful run");
        session.session_log_retention.omitted_entries = 2;
        session.clear_session_log();
        session.push_log(entries[2].clone(), Utc::now());
        tracker.observe_session(&session);
        assert!(tracker.proven());
        assert_eq!(tracker.receipts.len(), 1);
        assert!(tracker.streamed.is_empty());
    }

    #[test]
    fn focused_outcome_legacy_native_success_is_not_a_resolving_proof() {
        let mut session = outcome_session();
        let mut native = outcome_verifier(&session, "native-pass", 0, 0, false);
        native.1["output"].as_object_mut().expect("output").remove("executor");
        native.1["output"]["terminal_receipt"]["termination_origin"] = json!("process_exit");
        let mut no_binding = test_session("legacy-native");
        append_outcome(&mut no_binding, "native-runtime", vec![native.clone()]);
        assert!(super::TerminalOutcomeTracker::new(&no_binding).proven());
        let failed = outcome_verifier(&session, "fail", 0, 1, false);
        append_outcome(&mut session, "fail-runtime", vec![failed]);
        append_outcome(&mut session, "native-runtime", vec![native]);
        let tracker = super::TerminalOutcomeTracker::new(&session);
        assert!(!tracker.hard_veto);
        assert!(!tracker.proven());
    }

    #[test]
    fn terminal_evidence_requires_bound_known_command_outcomes_and_closed_effects() {
        let arguments = json!({"commands":[{"command_type":"shell_command"}]});
        let output = json!({"results":[{"command_type":"shell_command", "success":true,
            "output":{"exit_code":0, "stdout":"verified", "terminal_receipt":{
                "schema_version":"tura_command_terminal_receipt_v1",
                "outcome":"known", "terminal_state":"completed", "termination_proven":true,
                "process_reaped":true, "process_group_empty":true, "reconcile_required":false,
                "authoritative_publication":"unproven", "authority_effect":"none", "exit_code":null
            }}}]});
        let proven = |output: &serde_json::Value| super::terminal_tool_outcome_proven(
            "command_run", true, None, &arguments, output);
        assert!(proven(&output));
        for (pointer, key, value) in [
            ("/results/0", "success", json!(false)),
            ("/results/0", "command_type", json!("other")),
            ("/results/0/output", "exit_code", json!(7)),
            ("/results/0/output", "exit_code", serde_json::Value::Null),
            ("/results/0/output", "outcome", json!("unknown")),
            ("/results/0/output/terminal_receipt", "reconcile_required", json!(true)),
            ("/results/0/output/terminal_receipt", "process_reaped", json!(false)),
            ("/results/0/output/terminal_receipt", "process_group_empty", json!(false)),
            ("/results/0/output/terminal_receipt", "termination_proven", json!(false)),
            ("/results/0/output/terminal_receipt", "unresolved_effects", json!(["pending effect"])),
        ] {
            let mut invalid = output.clone();
            invalid.pointer_mut(pointer).expect("fixture path")[key] = value;
            assert!(!proven(&invalid), "{pointer}/{key} must veto terminal delivery");
        }
        let mut unknown = output.clone();
        unknown["results"][0].as_object_mut().expect("result").remove("success");
        assert!(!proven(&unknown));
        assert!(!proven(&json!({"results":[]})));
        assert!(!super::terminal_tool_outcome_proven("opaque", true, None, &arguments, &output));
        let mut incomplete = output;
        incomplete["results"][0]["output"]["terminal_receipt"].as_object_mut()
            .expect("receipt").remove("outcome");
        assert!(!proven(&incomplete));
    }

    #[test]
    fn provider_retry_progress_uses_fixed_bounded_stderr_markers() {
        let mut output = Vec::new();
        for phase in [
            ProviderRetryProgress::Wait,
            ProviderRetryProgress::Start,
            ProviderRetryProgress::Exhausted,
            ProviderRetryProgress::Terminal,
        ] {
            assert!(phase.line().len() <= 40);
            write_provider_retry_progress(&mut output, phase);
        }
        assert_eq!(
            output,
            b"[provider] phase=retry-wait\n[provider] phase=retry-start\n[provider] phase=retry-exhausted\n[provider] phase=terminal\n"
        );
    }

    #[test]
    fn provider_retry_progress_ignores_stderr_write_failure() {
        struct ClosedStderr;
        impl std::io::Write for ClosedStderr {
            fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
                Err(std::io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        write_provider_retry_progress(&mut ClosedStderr, ProviderRetryProgress::Wait);
    }

    #[test]
    fn sanitized_transport_failure_after_command_effect_keeps_retry_gate_closed() {
        let session = test_session("session-safe-transport-after-effect");
        let mut runtime = test_runtime_with_usage(&session, 0);
        let error = tura_llm_rust::TuraError::Network {
            message: "provider transport failure: phase=responses-sse category=decode cause=unexpected-eof"
                .to_string(),
        };
        crate::provider_flow::errors::finish_provider_call_failure_after_command_effect(
            &mut runtime,
            Utc::now(),
            &error,
            lifecycle::RuntimeState::Failed,
        )
        .expect("record transport failure after an accepted effect");

        assert!(
            !(runtime.state == lifecycle::RuntimeState::TimedOut
                || crate::provider_flow::errors::runtime_failure_allows_retry(&runtime))
        );
        let failure = runtime.error.as_ref().expect("runtime failure");
        assert_eq!(
            failure.error_code.as_deref(),
            Some("CALL_FAILED_AFTER_COMMAND_EFFECT")
        );
        assert!(!failure.retry_allowed);
        assert!(!failure.fallback_allowed);
        assert!(
            failure
                .error_text
                .as_deref()
                .unwrap()
                .contains("phase=responses-sse")
        );
    }

    #[test]
    fn provider_retry_progress_preserves_exact_input_retry_policy() {
        let session = test_session("session-safe-transport-exact-input");
        let mut runtime = test_runtime_with_usage(&session, 0);
        crate::provider_flow::errors::finish_runtime_failure_with_retry_policy(
            &mut runtime,
            Utc::now(),
            "OFFICIAL_CODEX_APP_SERVER_FAILED",
            "official Codex transport failed".to_string(),
            lifecycle::RuntimeState::Failed,
            true,
        )
        .expect("record exact-input retry");
        let mut output = Vec::new();
        write_provider_retry_progress(&mut output, ProviderRetryProgress::Wait);
        write_provider_retry_progress(&mut output, ProviderRetryProgress::Start);

        assert!(crate::provider_flow::errors::runtime_failure_allows_retry(&runtime));
        assert!(crate::provider_flow::errors::runtime_failure_requires_exact_input(&runtime));
        assert!(!runtime.error.as_ref().unwrap().fallback_allowed);
        assert_eq!(
            output,
            b"[provider] phase=retry-wait\n[provider] phase=retry-start\n"
        );
    }

    #[test]
    fn initial_context_prefix_keeps_only_developer_prefix_without_replaying_history() {
        let initial_messages = vec![
            json!({"role": "developer", "content": "permissions"}),
            json!({"role": "assistant", "content": "old answer"}),
            json!({"type": "function_call", "name": "command_run", "call_id": "call_old"}),
            json!({"type": "function_call_output", "call_id": "call_old", "output": "old output"}),
            json!({"role": "user", "content": "new task"}),
        ];
        let session_messages = vec![
            json!({"role": "user", "content": "new task"}),
            json!({"role": "assistant", "content": "new answer"}),
        ];

        let messages =
            messages_with_initial_context_prefix(&initial_messages, session_messages, "new task");

        assert_eq!(messages.len(), 3);
        assert_eq!(messages[0]["role"], "developer");
        assert!(!messages.iter().any(|message| {
            message.get("call_id").and_then(serde_json::Value::as_str) == Some("call_old")
        }));
    }

    #[test]
    fn initial_context_prefix_full_overlap_stays_stable_across_tool_turns() {
        let snapshot = json!({"role": "developer", "content": "snapshot"});
        let environment = json!({"role": "developer", "content": "environment"});
        let task = json!({"role": "user", "content": "task"});
        let initial = vec![snapshot.clone(), environment.clone(), task.clone()];
        let first = messages_with_initial_context_prefix(&initial, vec![task.clone()], "task");
        let second = messages_with_initial_context_prefix(
            &initial,
            vec![
                snapshot.clone(),
                environment.clone(),
                task.clone(),
                json!({"type": "function_call", "call_id": "call_1"}),
                json!({"type": "function_call_output", "call_id": "call_1"}),
            ],
            "task",
        );

        assert_eq!(first, vec![snapshot, environment, task]);
        assert_eq!(&second[..first.len()], first.as_slice());
        assert_eq!(second.len(), 5);
        assert_eq!(second[3]["call_id"], "call_1");
        assert_eq!(second[4]["type"], "function_call_output");
    }

    #[test]
    fn initial_context_prefix_merges_only_longest_exact_suffix_to_prefix_overlap() {
        let a = json!({"role": "developer", "content": "a"});
        let b = json!({"role": "developer", "content": "b"});
        let c = json!({"role": "developer", "content": "c"});
        let task = json!({"role": "user", "content": "task"});
        let messages = messages_with_initial_context_prefix(
            &[a.clone(), b.clone(), c.clone()],
            vec![b.clone(), c.clone(), task.clone()],
            "task",
        );
        assert_eq!(messages, vec![a, b, c, task]);
    }

    #[test]
    fn initial_context_prefix_keeps_empty_and_nonoverlapping_messages() {
        let developer = json!({"role": "developer", "content": "instructions"});
        let task = json!({"role": "user", "content": "task"});
        assert_eq!(
            messages_with_initial_context_prefix(&[], vec![task.clone()], "task"),
            vec![task.clone()]
        );
        assert_eq!(
            messages_with_initial_context_prefix(&[developer.clone()], vec![], "task"),
            vec![developer.clone()]
        );
        assert_eq!(
            messages_with_initial_context_prefix(&[developer.clone()], vec![task.clone()], "task"),
            vec![developer, task]
        );
    }

    #[test]
    fn initial_context_prefix_preserves_role_metadata_and_changed_content() {
        let developer = json!({"role": "developer", "content": "same", "version": 1});
        let user = json!({"role": "user", "content": "same", "version": 1});
        let changed_metadata = json!({"role": "developer", "content": "same", "version": 2});
        let changed_content = json!({"role": "developer", "content": "changed", "version": 1});
        for distinct in [user, changed_metadata, changed_content] {
            assert_eq!(
                messages_with_initial_context_prefix(
                    &[developer.clone()],
                    vec![distinct.clone()],
                    "task"
                ),
                vec![developer.clone(), distinct]
            );
        }
    }

    #[test]
    fn initial_context_prefix_keeps_repeated_instructions_after_call_result_boundary() {
        let developer = json!({"role": "developer", "content": "instructions"});
        let call = json!({"type": "function_call", "call_id": "call_1"});
        let result = json!({"type": "function_call_output", "call_id": "call_1"});
        let next_call = json!({"type": "function_call", "call_id": "call_2"});
        let session = vec![
            developer.clone(),
            call.clone(),
            result.clone(),
            developer.clone(),
            next_call.clone(),
        ];
        assert_eq!(
            messages_with_initial_context_prefix(&[developer.clone()], session.clone(), "task"),
            session
        );
        assert_eq!(
            messages_with_initial_context_prefix(
                &[developer.clone()],
                vec![
                    call.clone(),
                    result.clone(),
                    developer.clone(),
                    next_call.clone()
                ],
                "task"
            ),
            vec![
                developer,
                call,
                result,
                json!({"role": "developer", "content": "instructions"}),
                next_call
            ]
        );
    }

    #[test]
    fn manas_max_turns_uses_positive_env_override_and_ignores_invalid_values() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|error| error.into_inner());
        let previous = std::env::var_os("TURA_MANAS_MAX_TURNS");

        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_MANAS_MAX_TURNS", "3")
        };
        assert_eq!(manas_max_turns(), 3);

        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_MANAS_MAX_TURNS", "0")
        };
        assert_eq!(manas_max_turns(), DEFAULT_MANAS_MAX_TURNS);

        // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
        #[allow(
            unsafe_code,
            reason = "Rust 2024 process-environment mutation audited at the caller"
        )]
        unsafe {
            std::env::set_var("TURA_MANAS_MAX_TURNS", "not-a-number")
        };
        assert_eq!(manas_max_turns(), DEFAULT_MANAS_MAX_TURNS);

        if let Some(previous) = previous {
            // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::set_var("TURA_MANAS_MAX_TURNS", previous)
            };
        } else {
            // SAFETY: the caller ensures no concurrent foreign environment access races with this mutation.
            #[allow(
                unsafe_code,
                reason = "Rust 2024 process-environment mutation audited at the caller"
            )]
            unsafe {
                std::env::remove_var("TURA_MANAS_MAX_TURNS")
            };
        }
    }

    #[test]
    fn completed_turn_timestamp_does_not_regress_after_runtime_log_update() {
        let started_at = Utc::now() - Duration::minutes(5);
        let mut session = SessionManagement::new(
            "session-timestamp".to_string(),
            "Timestamp".to_string(),
            PathBuf::from("C:/workspace"),
            false,
            "coding".to_string(),
            SessionInput {
                user_input: "work".to_string(),
                file_input: vec![],
                agent: None,
                runtime_context: None,
                planning_mode_override: None,
            },
            "work".to_string(),
            started_at,
        );
        let runtime_log_at = Utc::now();
        session.push_log("runtime output", runtime_log_at);

        increment_turn_with_fresh_timestamp(&mut session);

        assert_eq!(session.session_current_turn, 1);
        assert!(
            session.session_last_update_at >= runtime_log_at,
            "turn completion must not restore the stale session start timestamp"
        );
    }

    #[test]
    fn non_planning_visible_reply_auto_completes_active_doing_task() {
        let mut session = test_session("session-auto-done");
        let mut task_plan = session.task_plan.clone();
        task_plan.detailed_tasks.push(TaskStep {
            task_id: "active".to_string(),
            step: 1,
            task_summary: "Answer directly".to_string(),
            status: PlanStatus::Doing,
            ..TaskStep::default()
        });
        session.replace_task_plan(task_plan, Utc::now());

        assert!(complete_active_doing_task_after_non_planning_reply(
            &mut session,
            true,
        ));

        assert_eq!(session.task_plan.detailed_tasks[0].status, PlanStatus::Done);
    }

    #[test]
    fn non_planning_without_visible_reply_keeps_active_doing_task_open() {
        let mut session = test_session("session-no-visible-reply");
        let mut task_plan = session.task_plan.clone();
        task_plan.detailed_tasks.push(TaskStep {
            task_id: "active".to_string(),
            step: 1,
            task_summary: "Wait for real output".to_string(),
            status: PlanStatus::Doing,
            ..TaskStep::default()
        });
        session.replace_task_plan(task_plan, Utc::now());

        assert!(!complete_active_doing_task_after_non_planning_reply(
            &mut session,
            false,
        ));

        assert_eq!(
            session.task_plan.detailed_tasks[0].status,
            PlanStatus::Doing
        );
    }

    #[test]
    fn non_planning_tool_turn_auto_completes_only_visible_status_only_doing() {
        assert!(!should_auto_complete_non_planning_doing_after_tool_turn(
            false,
            false,
            Some("doing"),
            true,
            false,
        ));
        assert!(!should_auto_complete_non_planning_doing_after_tool_turn(
            false,
            true,
            Some("doing"),
            true,
            false,
        ));
        assert!(!should_auto_complete_non_planning_doing_after_tool_turn(
            false,
            false,
            Some("doing"),
            false,
            false,
        ));
        assert!(!should_auto_complete_non_planning_doing_after_tool_turn(
            false,
            false,
            Some("doing"),
            true,
            true,
        ));
        assert!(!should_auto_complete_non_planning_doing_after_tool_turn(
            false,
            false,
            Some("done"),
            true,
            false,
        ));
        assert!(!should_auto_complete_non_planning_doing_after_tool_turn(
            true,
            false,
            Some("doing"),
            true,
            false,
        ));
    }

    #[test]
    fn no_tool_retry_uses_goal_mode_instead_of_planning_capability() {
        let mut session = test_session("session-goal-retry");

        assert!(!should_retry_no_tool_task_status(
            &session, true, true, false
        ));
        assert!(should_retry_no_tool_task_status(&session, true, true, true));
        assert!(!should_retry_no_tool_task_status(
            &session, false, true, true
        ));

        session.goal_mode = true;
        assert!(should_retry_no_tool_task_status(
            &session, false, true, false
        ));
        assert!(!should_retry_no_tool_task_status(
            &session, true, false, true
        ));
    }

    #[test]
    fn source_read_only_goal_mode_retries_only_with_admitted_task_status() {
        let mut session = test_session("session-source-read-goal-retry");
        session.goal_mode = true;
        let contract = json!({
            "allowed_operations": ["read", "command"],
            "denied_operations": ["create", "modify", "delete", "network"],
            "read_scopes": ["src/main.rs"],
            "write_scopes": [],
            "command_templates": [],
            "source_read": true
        });
        let commands = BTreeSet::from([
            "source_read".to_string(),
            "planning".to_string(),
            "task_status".to_string(),
        ]);
        let visible = provider_command_run_commands_for_jspace(&commands, Some(&contract));
        assert_eq!(visible, commands);
        assert!(should_retry_no_tool_task_status(
            &session,
            visible.contains("planning"),
            visible.contains("task_status"),
            false,
        ));

        let commands_without_status = BTreeSet::from(["source_read".to_string()]);
        let visible_without_status =
            provider_command_run_commands_for_jspace(&commands_without_status, Some(&contract));
        assert_eq!(visible_without_status, commands_without_status);
        assert!(!should_retry_no_tool_task_status(
            &session,
            visible_without_status.contains("planning"),
            visible_without_status.contains("task_status"),
            false,
        ));
    }

    #[test]
    fn no_tool_retry_budget_applies_to_goal_mode_too() {
        assert!(should_continue_no_tool_task_status_retry(0));
        assert!(!should_continue_no_tool_task_status_retry(u64::from(
            no_tool_retry_limit()
        )));
    }

    #[test]
    fn auto_compact_summary_triggers_when_new_context_estimate_exceeds_limit() {
        let mut session = test_session("session-auto-compact");
        session.context_tokens.input = 950;
        session.context_tokens.limit = 1_000;
        let runtime = test_runtime_with_usage(&session, 950);
        let tool_results = vec![ToolExecutionResult {
            tool_name: "command_run".to_string(),
            arguments: json!({"commands":[{"command_type":"shell_command","command_line":"probe"}]}),
            result: json!({"results":[{"step":1,"command_type":"shell_command","success":true,"output":"X".repeat(300)}]}),
            success: true,
            error: None,
        }];

        let summary = auto_compact_summary_after_new_context(&session, &runtime, &tool_results)
            .expect("new context should force automatic compaction");

        assert!(summary.contains("Automatic context checkpoint"));
        assert!(summary.contains("bytes/4"));
        assert!(summary.contains("active context limit 1000"));
    }

    #[test]
    fn auto_compact_summary_does_not_trigger_when_projection_fits_limit() {
        let mut session = test_session("session-auto-compact-fits");
        session.context_tokens.input = 100;
        session.context_tokens.limit = 1_000;
        let runtime = test_runtime_with_usage(&session, 100);

        assert!(auto_compact_summary_after_new_context(&session, &runtime, &[]).is_none());
    }

    #[test]
    fn no_tool_visible_reply_above_limit_falls_through_after_compaction() {
        let mut session = test_session("session-no-tool-compact-visible");
        session.context_tokens.input = 950;
        session.context_tokens.limit = 1_000;
        let mut runtime = test_runtime_with_usage(&session, 950);

        for (text, output) in [
            ("Done. ".repeat(64), None),
            (
                "<think>hidden</think>".to_string(),
                Some(json!({"text": "Done. ".repeat(64)})),
            ),
        ] {
            runtime.text = text;
            runtime.output = output;
            let publication = visible_runtime_reply(&runtime).map(|_| Ok::<(), &str>(()));
            let compacted = auto_compact_summary_after_new_context(&session, &runtime, &[]).is_some();

            assert!(publication.as_ref().is_some_and(Result::is_ok));
            assert!(compacted);
            assert!(!should_continue_after_no_tool_compaction(
                compacted,
                publication.as_ref(),
            ));
        }
    }

    #[test]
    fn no_tool_below_limit_ignores_publication_result_for_compaction_continuation() {
        let mut session = test_session("session-no-tool-compact-below-limit");
        session.context_tokens.input = 100;
        session.context_tokens.limit = 1_000;
        let mut runtime = test_runtime_with_usage(&session, 100);
        runtime.text = "Done. ".repeat(64);
        let compacted = auto_compact_summary_after_new_context(&session, &runtime, &[]).is_some();
        assert!(!compacted);

        for result in [Ok::<(), &str>(()), Err("publication failed")] {
            let publication = visible_runtime_reply(&runtime).map(|_| result);
            assert!(publication.is_some());
            assert!(!should_continue_after_no_tool_compaction(
                compacted,
                publication.as_ref(),
            ));
        }
    }

    #[test]
    fn no_tool_hidden_or_empty_reply_keeps_compaction_continuation() {
        let mut session = test_session("session-no-tool-compact-hidden");
        session.context_tokens.input = 1_100;
        session.context_tokens.limit = 1_000;
        let mut runtime = test_runtime_with_usage(&session, 1_100);

        for text in ["", " \t\r\n", "<think>hidden</think>", "<think>unfinished"] {
            for (runtime_text, output) in [(text, None), ("", Some(json!({"text": text})))] {
                runtime.text = runtime_text.to_string();
                runtime.output = output;
                let publication = visible_runtime_reply(&runtime).map(|_| -> Result<(), &str> {
                    panic!("hidden or empty replies must not attempt publication")
                });
                assert!(publication.is_none());
                assert!(auto_compact_summary_after_new_context(&session, &runtime, &[]).is_none());

                // These replies add no context tokens today. Exercise the production gate
                // with compaction already applied; missing publication must not be success.
                assert!(should_continue_after_no_tool_compaction(
                    true,
                    publication.as_ref(),
                ));
                assert!(!should_continue_after_no_tool_compaction(
                    false,
                    publication.as_ref(),
                ));
            }
        }
    }

    #[test]
    fn no_tool_visible_reply_publication_failure_keeps_compaction_continuation() {
        let mut session = test_session("session-no-tool-compact-publication-failed");
        session.context_tokens.input = 950;
        session.context_tokens.limit = 1_000;
        let mut runtime = test_runtime_with_usage(&session, 950);
        runtime.text = "Done. ".repeat(64);
        let publication =
            visible_runtime_reply(&runtime).map(|_| Err::<(), &str>("publication failed"));
        let compacted = auto_compact_summary_after_new_context(&session, &runtime, &[]).is_some();

        assert!(publication.as_ref().is_some_and(Result::is_err));
        assert!(compacted);
        assert!(should_continue_after_no_tool_compaction(
            compacted,
            publication.as_ref(),
        ));
    }

    #[test]
    fn visible_terminal_status_skips_backfill_for_non_whitespace_replies() {
        let status_result = ToolExecutionResult {
            tool_name: "command_run".to_string(),
            arguments: json!({"commands":[{"command_type":"task_status"}]}),
            result: json!({"results":[{
                "command_type":"task_status",
                "success":true,
                "output":{"task_status":{"status":"done"}}
            }]}),
            success: true,
            error: None,
        };
        let question_status_result = ToolExecutionResult {
            result: json!({"results":[{
                "command_type":"task_status",
                "success":true,
                "output":{"task_status":{"status":"question"}}
            }]}),
            ..status_result.clone()
        };
        let doing_status_result = ToolExecutionResult {
            result: json!({"results":[{
                "command_type":"task_status",
                "success":true,
                "output":{"task_status":{"status":"doing"}}
            }]}),
            ..status_result.clone()
        };
        let real_reply = concat!(
            "The patch is complete, and I reviewed the fresh source readback. ",
            "The unchanged public verifier passed all 50 tests with exit code 0. ",
            "Held-out acceptance remains with the parent."
        );

        for reply in [real_reply, "Done.", "完", " \tDone.\u{2003} "] {
            assert!(should_end_turn_without_task_status_backfill(
                std::slice::from_ref(&status_result),
                Some("done"),
                Some(reply),
                true,
            ));
        }
        assert!(!should_end_turn_without_task_status_backfill(
            std::slice::from_ref(&status_result),
            Some("done"),
            Some(real_reply),
            false,
        ));
        assert!(should_end_turn_without_task_status_backfill(
            std::slice::from_ref(&question_status_result),
            Some("question"),
            Some("Which file should I use?"),
            true,
        ));
        assert!(!should_end_turn_without_task_status_backfill(
            std::slice::from_ref(&doing_status_result),
            Some("doing"),
            Some("Done."),
            true,
        ));

        for status in [None, Some("question"), Some("doing")] {
            assert!(!should_end_turn_without_task_status_backfill(
                std::slice::from_ref(&status_result),
                status,
                Some("Done."),
                true,
            ));
        }

        for command_success in [true, false] {
            let mut with_command = status_result.clone();
            with_command.result = json!({"results":[
                {"command_type":"shell_command","success":command_success,"output":"command result"},
                {"command_type":"task_status","success":true,"output":{"task_status":{"status":"done"}}}
            ]});
            assert!(!should_end_turn_without_task_status_backfill(
                &[with_command],
                Some("done"),
                Some(real_reply),
                true,
            ));
        }

        let failed_result = ToolExecutionResult {
            result: json!({"results":[{
                "command_type":"task_status",
                "success":false,
                "output":{"task_status":{"status":"done"}}
            }]}),
            success: false,
            error: Some("task_status failed".to_string()),
            ..status_result.clone()
        };
        assert!(!should_end_turn_without_task_status_backfill(
            &[failed_result],
            Some("done"),
            Some(real_reply),
            true,
        ));
        let wrong_tool_result = ToolExecutionResult {
            tool_name: "task_status".to_string(),
            ..status_result.clone()
        };
        assert!(!should_end_turn_without_task_status_backfill(
            &[wrong_tool_result],
            Some("done"),
            Some(real_reply),
            true,
        ));
        assert!(!should_end_turn_without_task_status_backfill(
            &[status_result.clone(), status_result],
            Some("done"),
            Some(real_reply),
            true,
        ));
        assert!(!should_end_turn_without_task_status_backfill(
            &[],
            Some("done"),
            Some(real_reply),
            true,
        ));
    }

    #[test]
    fn terminal_shortcut_requires_visible_runtime_reply_and_publication() {
        let session = test_session("session-terminal-visible-reply");
        let mut runtime = test_runtime_with_usage(&session, 0);

        for status in ["done", "question"] {
            let status_result = ToolExecutionResult {
                tool_name: "command_run".to_string(),
                arguments: json!({"commands":[{"command_type":"task_status"}]}),
                result: json!({"results":[{
                    "command_type":"task_status",
                    "success":true,
                    "output":{"task_status":{"status":status}}
                }]}),
                success: true,
                error: None,
            };

            for (text, expected) in [
                ("<think>hidden</think>", None),
                ("<think>unfinished", None),
                (r#"<think>{"reply_message":"hidden"}</think>"#, None),
                (r#"<think>{"reply_message":"hidden"}"#, None),
                (r#"{"reply_message":"<think>hidden</think>"}"#, None),
                ("Done.", Some("Done.")),
                ("完", Some("完")),
                (r#"{"answer":42}"#, Some(r#"{"answer":42}"#)),
                (r#"{"reply_message":"Done."}"#, Some("Done.")),
                ("<think>hidden</think>Done.", Some("Done.")),
                ("Done.<think>unfinished", Some("Done.")),
            ] {
                for (runtime_text, output) in [
                    (text, None),
                    ("", Some(json!({"text": text}))),
                    ("<think>hidden</think>", Some(json!({"text": text}))),
                ] {
                    runtime.text = runtime_text.to_string();
                    runtime.output = output;
                    let visible = visible_runtime_reply(&runtime);
                    assert_eq!(visible.as_deref(), expected, "{status}: {text:?}");

                    for published in [false, true] {
                        assert_eq!(
                            should_end_turn_without_task_status_backfill(
                                std::slice::from_ref(&status_result),
                                Some(status),
                                visible.as_deref(),
                                published,
                            ),
                            expected.is_some() && published,
                            "{status}: {text:?}, published={published}"
                        );
                    }
                    if visible.is_none() {
                        assert!(terminal_status_needs_final_response_turn(
                            Some(status),
                            false,
                            false,
                        ));
                    }
                }
            }
        }
    }

    #[test]
    fn empty_or_missing_terminal_reply_requires_backfill() {
        let ascii_whitespace = " \t\r\n".repeat(64);
        let unicode_whitespace = concat!(
            "\u{0085}\u{00a0}\u{1680}\u{2003}",
            "\u{2028}\u{2029}\u{202f}\u{205f}\u{3000}"
        )
        .repeat(16);

        for status in ["done", "question"] {
            let status_result = ToolExecutionResult {
                tool_name: "command_run".to_string(),
                arguments: json!({"commands":[{"command_type":"task_status"}]}),
                result: json!({"results":[{
                    "command_type":"task_status",
                    "success":true,
                    "output":{"task_status":{"status":status}}
                }]}),
                success: true,
                error: None,
            };

            for reply in [
                None,
                Some(""),
                Some(" \t\r\n"),
                Some(ascii_whitespace.as_str()),
                Some("\u{2003}"),
                Some(unicode_whitespace.as_str()),
            ] {
                assert!(!should_end_turn_without_task_status_backfill(
                    std::slice::from_ref(&status_result),
                    Some(status),
                    reply,
                    true,
                ));
            }
        }
    }

    #[test]
    fn unpublished_terminal_replies_require_backfill() {
        let substantive_reply = concat!(
            "The scoped runtime change preserves the existing terminal-status gates and ",
            "requires a successful publisher acknowledgement before reusing a visible reply. ",
            "A failed publication still needs a user-facing response; validation and acceptance ",
            "remain with the parent."
        );

        for status in ["done", "question"] {
            let status_result = ToolExecutionResult {
                tool_name: "command_run".to_string(),
                arguments: json!({"commands":[{"command_type":"task_status"}]}),
                result: json!({"results":[{
                    "command_type":"task_status",
                    "success":true,
                    "output":{"task_status":{"status":status}}
                }]}),
                success: true,
                error: None,
            };

            for reply in [substantive_reply, "Done.", "Which file should I use?"] {
                assert!(should_end_turn_without_task_status_backfill(
                    std::slice::from_ref(&status_result),
                    Some(status),
                    Some(reply),
                    true,
                ));
                assert!(!should_end_turn_without_task_status_backfill(
                    std::slice::from_ref(&status_result),
                    Some(status),
                    Some(reply),
                    false,
                ));
            }
        }
    }

    #[test]
    fn terminal_status_requests_final_response_when_backfill_is_still_needed() {
        assert!(terminal_status_needs_final_response_turn(
            Some("done"),
            true,
            false,
        ));
        assert!(terminal_status_needs_final_response_turn(
            Some("done"),
            false,
            false,
        ));
        assert!(terminal_status_needs_final_response_turn(
            Some("done"),
            true,
            true,
        ));
        assert!(!terminal_status_needs_final_response_turn(
            Some("question"),
            true,
            false,
        ));
        assert!(terminal_status_needs_final_response_turn(
            Some("question"),
            false,
            false,
        ));
        assert!(terminal_status_needs_final_response_turn(
            Some("question"),
            true,
            true,
        ));
        assert!(!terminal_status_needs_final_response_turn(
            Some("doing"),
            false,
            true,
        ));
        assert!(!terminal_status_needs_final_response_turn(None, true, true));
    }

    #[test]
    fn terminal_checkpoint_commit_skips_when_command_run_sandbox_is_enabled() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|error| error.into_inner());
        let _sandbox = EnvGuard::set("TURA_COMMAND_RUN_SANDBOX", "enabled");
        let temp = tempfile::tempdir().expect("temp workspace");
        std::fs::write(temp.path().join("src.txt"), "sandboxed change").expect("fixture file");
        let mut session = test_session("session-sandbox-checkpoint");
        session.session_directory = temp.path().to_path_buf();

        assert!(!commit_terminal_session_checkpoint(
            &session,
            SessionState::Completed,
            true
        ));
        assert!(
            !temp.path().join(".git").exists(),
            "sandboxed runtime completion must not initialize git or create checkpoint commits"
        );
    }

    #[test]
    fn terminal_checkpoint_commit_requires_enabled_setting_success_and_task_status_done() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|error| error.into_inner());
        let _sandbox = EnvGuard::set("TURA_COMMAND_RUN_SANDBOX", "disabled");

        {
            let _auto_commit = EnvGuard::set("TURA_RUNTIME_AUTO_GIT_COMMIT", "0");
            let temp = tempfile::tempdir().expect("disabled auto-commit workspace");
            let mut session = test_session("session-auto-commit-disabled");
            session.session_directory = temp.path().to_path_buf();
            assert!(!commit_terminal_session_checkpoint(
                &session,
                SessionState::Completed,
                true
            ));
            assert!(!temp.path().join(".git").exists());
        }

        let _auto_commit = EnvGuard::set("TURA_RUNTIME_AUTO_GIT_COMMIT", "1");
        let failed = tempfile::tempdir().expect("failed runtime workspace");
        let mut failed_session = test_session("session-auto-commit-failed");
        failed_session.session_directory = failed.path().to_path_buf();
        assert!(!commit_terminal_session_checkpoint(
            &failed_session,
            SessionState::Failed,
            true
        ));
        assert!(!failed.path().join(".git").exists());

        let no_done = tempfile::tempdir().expect("missing done marker workspace");
        let mut no_done_session = test_session("session-auto-commit-no-done");
        no_done_session.session_directory = no_done.path().to_path_buf();
        assert!(!commit_terminal_session_checkpoint(
            &no_done_session,
            SessionState::Completed,
            false
        ));
        assert!(!no_done.path().join(".git").exists());

        let completed = tempfile::tempdir().expect("completed auto-commit workspace");
        std::fs::write(completed.path().join("change.txt"), "done").expect("workspace change");
        let mut completed_session = test_session("session-auto-commit-done");
        completed_session.session_directory = completed.path().to_path_buf();
        assert!(commit_terminal_session_checkpoint(
            &completed_session,
            SessionState::Completed,
            true
        ));
        assert!(completed.path().join(".git").exists());
    }

    fn test_session(id: &str) -> SessionManagement {
        let now = Utc::now();
        SessionManagement::new(
            id.to_string(),
            "Test".to_string(),
            PathBuf::from("C:/workspace"),
            false,
            "coding".to_string(),
            SessionInput {
                user_input: "work".to_string(),
                file_input: vec![],
                agent: None,
                runtime_context: None,
                planning_mode_override: None,
            },
            "work".to_string(),
            now,
        )
    }

    fn test_runtime_with_usage(session: &SessionManagement, input_tokens: u64) -> RuntimeAggregate {
        let mut runtime = RuntimeAggregate::new(
            format!("runtime-{}", session.session_id),
            session.session_id.clone(),
            "agent-test".to_string(),
            RuntimeProviderConfig {
                base: ProviderConfig {
                    tura_llm_name: "provider".to_string(),
                    default_model_tier: None,
                    current_model: None,
                    stream: false,
                    temperature: 0.0,
                    max_tokens: 0,
                    tool_choice: ToolChoice::Auto,
                    time_out_ms: 120_000,
                },
                thinking: false,
                provider_name: "provider".to_string(),
                model_name: "model".to_string(),
                provider_url_name: "provider".to_string(),
                llm_provider_name: "provider".to_string(),
            },
            Utc::now(),
        );
        runtime
            .update_usage(Some(UsageReport {
                input_tokens,
                output_tokens: 0,
                total_tokens: input_tokens,
                cached_input_tokens: 0,
                cache_write_tokens: 0,
                reasoning_tokens: 0,
                attachment_input_tokens: 0,
                input_cost: 0.0,
                output_cost: 0.0,
                total_cost: 0.0,
                currency: "USD".to_string(),
                pricing_source: "test".to_string(),
                latency_ms: 0,
                time_to_first_token_ms: 0,
                token_per_second: 0.0,
            }))
            .expect("fixture usage should apply");
        runtime
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
